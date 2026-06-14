"""Tests for the self-describing JSON parser."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from data_contract.data_parsers.json_parser import JsonParser
from data_contract.errors import ConfigError


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _default_payload(rows: list[dict] | None = None) -> dict:
    """A minimal example of the report_header / report_row shape the
    parser was built for."""
    if rows is None:
        rows = [
            {"c1": "alpha", "c2": "1"},
            {"c1": "beta",  "c2": "2"},
            {"c1": "gamma", "c2": "3"},
        ]
    return {
        "data": {
            "report_header": {
                "c1": {"name": "column 1", "type": "java.lang.String"},
                "c2": {"name": "column 2", "type": "java.lang.String"},
            },
            "report_row": rows,
        }
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_json_single_file_renames_columns_and_attributes_source(tmp_path, json_params):
    p = _write_json(tmp_path / "a.json", _default_payload())
    parser = JsonParser(json_params())
    df = parser.read([p]).frame.collect()
    assert df.height == 3
    # Columns renamed via report_header.name -- contract-field-name shape.
    assert {"column 1", "column 2"}.issubset(df.columns)
    assert df["column 1"].to_list() == ["alpha", "beta", "gamma"]
    assert df["column 2"].to_list() == ["1", "2", "3"]
    # Synthetic columns required by the runner.
    assert df["__source_file__"].to_list() == ["a.json"] * 3
    assert df["__row_index__"].to_list() == [1, 2, 3]


def test_json_multi_file_concat_diagonal(tmp_path, json_params):
    # File A has columns 1 + 2; file B has columns 1 + 3 (different code maps).
    a = _write_json(tmp_path / "a.json", _default_payload(rows=[{"c1": "a", "c2": "1"}]))
    b_payload = {
        "data": {
            "report_header": {
                "c1": {"name": "column 1"},
                "c3": {"name": "column 3"},
            },
            "report_row": [{"c1": "b", "c3": "x"}],
        }
    }
    b = _write_json(tmp_path / "b.json", b_payload)
    parser = JsonParser(json_params())
    df = parser.read([a, b]).frame.collect()
    assert df.height == 2
    assert {"column 1", "column 2", "column 3"}.issubset(df.columns)
    by_file = dict(zip(df["__source_file__"].to_list(), df["column 1"].to_list()))
    assert by_file == {"a.json": "a", "b.json": "b"}
    # Each file's "missing" column shows up as null.
    row_a = df.filter(pl.col("__source_file__") == "a.json").to_dicts()[0]
    row_b = df.filter(pl.col("__source_file__") == "b.json").to_dicts()[0]
    assert row_a["column 3"] is None
    assert row_b["column 2"] is None
    # Row index resets per file.
    assert row_a["__row_index__"] == 1
    assert row_b["__row_index__"] == 1


# ---------------------------------------------------------------------------
# Empty + edge shapes
# ---------------------------------------------------------------------------


def test_json_empty_report_row_yields_zero_rows_with_header_columns(tmp_path, json_params):
    p = _write_json(tmp_path / "a.json", _default_payload(rows=[]))
    df = JsonParser(json_params()).read([p]).frame.collect()
    assert df.height == 0
    assert {"column 1", "column 2"}.issubset(df.columns)


def test_json_missing_rows_path_treated_as_empty(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "column 1"}},
            # report_row deliberately absent
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    assert df.height == 0
    assert "column 1" in df.columns


def test_json_all_data_columns_are_string_dtype(tmp_path, json_params):
    """Match the CSV / Excel invariant: every column is pl.String at parser
    output time. Type validation is the contract layer's job."""
    p = _write_json(tmp_path / "a.json", _default_payload())
    df = JsonParser(json_params()).read([p]).frame.collect()
    for col in ("column 1", "column 2"):
        assert df.schema[col] == pl.String, f"column {col!r} dtype is {df.schema[col]!r}"


# ---------------------------------------------------------------------------
# Stringification of non-string scalars and nested values
# ---------------------------------------------------------------------------


def test_json_bool_and_number_scalars_stringify(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {
                "c1": {"name": "flag"},
                "c2": {"name": "amount"},
                "c3": {"name": "tally"},
            },
            "report_row": [{"c1": True, "c2": 12.5, "c3": 42}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    row = df.to_dicts()[0]
    assert row["flag"] == "true"
    assert row["amount"] == "12.5"
    assert row["tally"] == "42"


def test_json_nested_value_stringified_via_json_dumps(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "nested"}},
            "report_row": [{"c1": {"foo": 1, "bar": [2, 3]}}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    val = df["nested"].to_list()[0]
    # Round-trips back to the same Python object.
    assert json.loads(val) == {"foo": 1, "bar": [2, 3]}


def test_json_null_value_preserved_as_none(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "n"}},
            "report_row": [{"c1": None}, {"c1": "kept"}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    assert df["n"].to_list() == [None, "kept"]


def test_json_null_tokens_become_nulls(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "n"}},
            "report_row": [
                {"c1": "NULL"},
                {"c1": "null"},
                {"c1": ""},
                {"c1": "kept"},
            ],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    assert df["n"].to_list() == [None, None, None, "kept"]


# ---------------------------------------------------------------------------
# Resilience: row contains a code the header doesn't declare
# ---------------------------------------------------------------------------


def test_json_row_with_unmapped_code_passes_through_as_code(tmp_path, json_params):
    """The validation layer will flag the unrenamed column as `extra_column`
    -- that's the right behaviour, so the parser should pass through rather
    than error."""
    payload = {
        "data": {
            "report_header": {"c1": {"name": "column 1"}},
            "report_row": [{"c1": "a", "c99": "extra"}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    # `c99` is the column name because no header entry described it.
    assert df.height == 1
    assert df["c99"].to_list() == ["extra"]


def test_json_header_entry_without_name_key_falls_back_to_code(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {
                "c1": {"name": "column 1"},
                "c2": {"type": "string"},   # no `name`
            },
            "report_row": [{"c1": "a", "c2": "b"}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    df = JsonParser(json_params()).read([p]).frame.collect()
    # c2 surfaces under its code because the header didn't give it a name.
    assert df["column 1"].to_list() == ["a"]
    assert df["c2"].to_list() == ["b"]


# ---------------------------------------------------------------------------
# Configurable paths -- the parser handles non-default JSON shapes
# ---------------------------------------------------------------------------


def test_json_custom_header_and_rows_paths(tmp_path, json_params):
    payload = {
        "result": {
            "columns": {
                "c1": {"label": "column 1"},
                "c2": {"label": "column 2"},
            },
            "rows": [{"c1": "a", "c2": "b"}],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    parser = JsonParser(json_params(
        header_path="result.columns",
        rows_path="result.rows",
        name_key="label",
    ))
    df = parser.read([p]).frame.collect()
    assert df["column 1"].to_list() == ["a"]
    assert df["column 2"].to_list() == ["b"]


# ---------------------------------------------------------------------------
# Errors with clear messages
# ---------------------------------------------------------------------------


def test_json_missing_header_path_raises(tmp_path, json_params):
    payload = {"data": {"report_row": []}}   # no report_header
    p = _write_json(tmp_path / "a.json", payload)
    # Error names the offending segment AND the keys actually present at that
    # depth so the user can fix either the YAML or the JSON.
    with pytest.raises(ConfigError, match="'report_header' missing"):
        JsonParser(json_params()).read([p]).frame


def test_json_header_path_wrong_type_raises(tmp_path, json_params):
    payload = {"data": {"report_header": ["not", "a", "dict"], "report_row": []}}
    p = _write_json(tmp_path / "a.json", payload)
    with pytest.raises(ConfigError, match="expected a dict"):
        JsonParser(json_params()).read([p]).frame


def test_json_rows_path_wrong_type_raises(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "n"}},
            "report_row": {"not": "a list"},
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    with pytest.raises(ConfigError, match="expected a list"):
        JsonParser(json_params()).read([p]).frame


def test_json_row_not_an_object_raises(tmp_path, json_params):
    payload = {
        "data": {
            "report_header": {"c1": {"name": "n"}},
            "report_row": ["not-a-dict"],
        }
    }
    p = _write_json(tmp_path / "a.json", payload)
    with pytest.raises(ConfigError, match="must be an object"):
        JsonParser(json_params()).read([p]).frame


def test_json_missing_required_param_raises_with_clear_hint(tmp_path):
    """Bypass the fixture so neither header_path nor rows_path is supplied."""
    p = _write_json(tmp_path / "a.json", _default_payload())
    with pytest.raises(ConfigError, match="header_path.*rows_path"):
        JsonParser({}).read([p]).frame
