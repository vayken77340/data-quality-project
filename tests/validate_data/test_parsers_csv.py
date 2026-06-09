from __future__ import annotations

from pathlib import Path

import polars as pl

from data_contract.validate_data.parsers.csv import CsvParser


def _write_csv(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def test_csv_single_file_row_count_and_source_columns(tmp_path):
    p = _write_csv(tmp_path / "a.csv", "id,name\n1,alpha\n2,beta\n3,gamma\n")
    parser = CsvParser()
    lf = parser.read([p])
    df = lf.collect()
    assert df.height == 3
    assert "__source_file__" in df.columns
    assert "__row_index__" in df.columns
    assert df["__source_file__"].to_list() == ["a.csv"] * 3
    assert df["__row_index__"].to_list() == [1, 2, 3]


def test_csv_multi_file_concat_attributes_source(tmp_path):
    p1 = _write_csv(tmp_path / "jan.csv", "id,name\n1,a\n2,b\n")
    p2 = _write_csv(tmp_path / "feb.csv", "id,name\n3,c\n4,d\n")
    parser = CsvParser()
    df = parser.read([p1, p2]).collect()
    assert df.height == 4
    by_file = df.group_by("__source_file__").len().to_dict(as_series=False)
    counts = dict(zip(by_file["__source_file__"], by_file["len"]))
    assert counts == {"jan.csv": 2, "feb.csv": 2}
    # Row indices reset per file (1-based within each).
    rows = df.select("__source_file__", "__row_index__").to_dicts()
    by_file_rows = {}
    for r in rows:
        by_file_rows.setdefault(r["__source_file__"], []).append(r["__row_index__"])
    assert sorted(by_file_rows["jan.csv"]) == [1, 2]
    assert sorted(by_file_rows["feb.csv"]) == [1, 2]


def test_csv_custom_delimiter(tmp_path):
    p = _write_csv(tmp_path / "semi.csv", "id;name\n1;a\n2;b\n")
    parser = CsvParser({"delimiter": ";"})
    df = parser.read([p]).collect()
    assert df.height == 2
    # Every data column reads as String. Type validation happens via the
    # contract layer, not Polars's inferer.
    assert df["id"].to_list() == ["1", "2"]
    assert df["name"].to_list() == ["a", "b"]


def test_csv_null_tokens_become_nulls(tmp_path):
    p = _write_csv(tmp_path / "n.csv", "id,note\n1,NULL\n2,present\n3,\n")
    parser = CsvParser({"null_tokens": ["", "NULL"]})
    df = parser.read([p]).collect()
    notes = df["note"].to_list()
    assert notes == [None, "present", None]


def test_csv_header_row_offset(tmp_path):
    p = _write_csv(
        tmp_path / "headered.csv",
        "preface line\nblank\nid,name\n1,a\n2,b\n",
    )
    parser = CsvParser({"header_row": 3})
    df = parser.read([p]).collect()
    assert df.height == 2
    assert {"id", "name"}.issubset(df.columns)
    assert df["id"].to_list() == ["1", "2"]
    assert df["name"].to_list() == ["a", "b"]


def test_csv_all_data_columns_are_string_dtype(tmp_path):
    """The contract-enforced pipeline depends on every data column being
    String at parser-read time. This is the contract between Step 4a/4b and
    everything downstream."""
    p = _write_csv(tmp_path / "mixed.csv", "i,f,d,b,s\n1,1.5,2024-01-15,true,abc\n2,3.14,15/01/2024,false,xyz\n")
    df = CsvParser().read([p]).collect()
    for col in ("i", "f", "d", "b", "s"):
        assert df.schema[col] == pl.String, f"column {col!r} dtype is {df.schema[col]!r}"


def test_csv_leading_zero_preserved(tmp_path):
    """A CSV cell with `00042` reads back as `'00042'` -- no integer
    coercion, leading zeros intact. This is the regression guard for the
    previous Polars-inference behaviour that would have produced `42`."""
    p = _write_csv(tmp_path / "zeros.csv", "code\n00042\n00007\n")
    df = CsvParser().read([p]).collect()
    assert df["code"].dtype == pl.String
    assert df["code"].to_list() == ["00042", "00007"]


def test_csv_french_comma_preserved_for_contract_layer(tmp_path):
    """A CSV cell with `12,34` reads back verbatim. The contract layer
    (check_type_coercion against DOUBLE) is responsible for rejecting it."""
    p = _write_csv(tmp_path / "fr.csv", "val\n12,34\n1.5\n", )
    df = CsvParser({"delimiter": ";"}).read([p]).collect()
    # Delimiter is ; so the cell `12,34` survives as a single cell.
    assert df["val"].to_list() == ["12,34", "1.5"]
