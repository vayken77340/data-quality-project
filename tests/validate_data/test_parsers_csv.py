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
    assert df["id"].to_list() == [1, 2]
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
    assert df["id"].to_list() == [1, 2]
    assert df["name"].to_list() == ["a", "b"]
