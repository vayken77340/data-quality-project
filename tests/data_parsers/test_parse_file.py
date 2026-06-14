"""Tests for the two-tier parser API: `parse_file()` per file +
`FileParser.read()` framework wrapper that adds tracking columns and
multi-file concat.

These tests cover the cross-cutting framework behaviour (forced
`pl.String` dtype, `__source_file__` / `__row_index__` attachment,
`diagonal_relaxed` concat) so individual parser tests can focus on
format-specific logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from data_contract.data_parsers import (
    FileParser,
    ParsedFile,
    ParserSchema,
    ReadResult,
)
from data_contract.data_parsers.csv import CsvParser
from data_contract.data_parsers.excel import ExcelParser
from data_contract.data_parsers.json_parser import JsonParser


# ---------------------------------------------------------------------------
# parse_file() shape per built-in parser
# ---------------------------------------------------------------------------


def test_csv_parse_file_returns_parsed_file_with_column_names(tmp_path, csv_params):
    p = tmp_path / "a.csv"
    p.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")
    parsed = CsvParser(csv_params()).parse_file(p)
    assert isinstance(parsed, ParsedFile)
    assert isinstance(parsed.schema, ParserSchema)
    assert parsed.schema.column_names == ["id", "name"]
    # CSV doesn't declare types.
    assert parsed.schema.column_types is None


def test_excel_parse_file_returns_parsed_file_with_column_names(tmp_path, excel_params):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "T"
    ws.append(["id", "label"])
    ws.append([1, "a"])
    p = tmp_path / "a.xlsx"
    wb.save(p)
    parsed = ExcelParser(excel_params()).parse_file(p)
    assert isinstance(parsed.schema, ParserSchema)
    assert parsed.schema.column_names == ["id", "label"]
    assert parsed.schema.column_types is None


def test_json_parse_file_exposes_names_and_types(tmp_path, json_params):
    payload = {
        "data": [{
            "report_header": {
                "c1": {"name": "column 1", "type": "java.lang.String"},
                "c2": {"name": "column 2", "type": "java.lang.Integer"},
            },
            "report_row": [{"c1": "alpha", "c2": "1"}],
        }]
    }
    p = tmp_path / "a.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    parsed = JsonParser(json_params()).parse_file(p)
    assert parsed.schema.column_names == ["column 1", "column 2"]
    assert parsed.schema.column_types == {
        "column 1": "java.lang.String",
        "column 2": "java.lang.Integer",
    }


# ---------------------------------------------------------------------------
# normalize_source_type() per-parser
# ---------------------------------------------------------------------------


def test_json_parser_normalises_known_java_types(json_params):
    p = JsonParser(json_params())
    assert p.normalize_source_type("java.lang.String") == "string"
    assert p.normalize_source_type("java.lang.Integer") == "int32"
    assert p.normalize_source_type("java.lang.Long") == "int64"
    assert p.normalize_source_type("java.lang.Boolean") == "boolean"
    assert p.normalize_source_type("java.util.Date") == "date"


def test_json_parser_returns_none_for_unknown_source_type(json_params):
    assert JsonParser(json_params()).normalize_source_type("unknown.Type") is None


def test_csv_parser_normalize_source_type_returns_none(csv_params):
    """CSV doesn't carry types; the default normaliser returns None."""
    assert CsvParser(csv_params()).normalize_source_type("anything") is None


# ---------------------------------------------------------------------------
# FileParser.read() -- framework wrapper invariants
# ---------------------------------------------------------------------------


class _DictRowsParser(FileParser):
    """Minimal parser used to exercise the iterable-of-dicts path on the
    framework's `read()` without depending on a real file format.

    Spec params:    none.
    Reads via:      a captured `rows_per_path` dict (file basename -> rows).
    Multi-file:     handled by FileParser.read().
    """

    name = "_dict_rows_test"
    extensions = (".tst",)
    PARSER_PARAMS = ()

    def __init__(self, rows_per_path: dict[str, list[dict]],
                 schema: ParserSchema | None = None) -> None:
        super().__init__(params={})
        self._rows_per_path = rows_per_path
        self._schema = schema

    def parse_file(self, path, *, table_name_hint=None):
        return ParsedFile(rows=self._rows_per_path[path.name], schema=self._schema)


def _touch(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("", encoding="utf-8")
    return p


def test_read_attaches_tracking_columns(tmp_path):
    rows = [{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}]
    parser = _DictRowsParser(
        rows_per_path={"a.tst": rows},
        schema=ParserSchema(column_names=["id", "name"]),
    )
    result = parser.read([_touch(tmp_path, "a.tst")])
    assert isinstance(result, ReadResult)
    df = result.frame.collect()
    assert df["__source_file__"].to_list() == ["a.tst"] * 2
    assert df["__row_index__"].to_list() == [1, 2]


def test_read_forces_every_data_column_to_string_dtype(tmp_path):
    rows = [{"id": "1", "name": "alpha"}]
    parser = _DictRowsParser(
        rows_per_path={"a.tst": rows},
        schema=ParserSchema(column_names=["id", "name"]),
    )
    df = parser.read([_touch(tmp_path, "a.tst")]).frame.collect()
    assert df.schema["id"] == pl.String
    assert df.schema["name"] == pl.String


def test_read_multi_file_uses_diagonal_relaxed_concat(tmp_path):
    """File A has columns id,name; file B has columns id,city. Concat
    yields a 4-column union with nulls in the missing slot per row."""
    parser = _DictRowsParser(
        rows_per_path={
            "a.tst": [{"id": "1", "name": "alpha"}],
            "b.tst": [{"id": "2", "city": "Paris"}],
        },
        schema=None,
    )
    df = parser.read([_touch(tmp_path, "a.tst"), _touch(tmp_path, "b.tst")]).frame.collect()
    assert df.height == 2
    assert {"id", "name", "city"}.issubset(df.columns)
    row_a = df.filter(pl.col("__source_file__") == "a.tst").to_dicts()[0]
    row_b = df.filter(pl.col("__source_file__") == "b.tst").to_dicts()[0]
    assert row_a["city"] is None
    assert row_b["name"] is None
    # Row index resets per file.
    assert row_a["__row_index__"] == 1
    assert row_b["__row_index__"] == 1


def test_read_empty_rows_yields_zero_row_frame_with_declared_columns(tmp_path):
    parser = _DictRowsParser(
        rows_per_path={"a.tst": []},
        schema=ParserSchema(column_names=["id", "name"]),
    )
    df = parser.read([_touch(tmp_path, "a.tst")]).frame.collect()
    assert df.height == 0
    assert {"id", "name", "__source_file__", "__row_index__"}.issubset(df.columns)


def test_read_returns_per_file_schemas_in_order(tmp_path):
    schema_a = ParserSchema(column_names=["id"])
    schema_b = ParserSchema(column_names=["id", "city"])
    parser = _DictRowsParser(
        rows_per_path={
            "a.tst": [{"id": "1"}],
            "b.tst": [{"id": "2", "city": "Paris"}],
        },
        schema=schema_a,
    )
    # Override the schema returned per file by patching parse_file.
    def _per_file(path, *, table_name_hint=None):
        rows = parser._rows_per_path[path.name]
        return ParsedFile(
            rows=rows,
            schema=schema_a if path.name == "a.tst" else schema_b,
        )
    parser.parse_file = _per_file   # type: ignore[method-assign]

    paths = [_touch(tmp_path, "a.tst"), _touch(tmp_path, "b.tst")]
    result = parser.read(paths)
    assert [name for name, _ in result.per_file_schemas] == ["a.tst", "b.tst"]
    assert [sch for _, sch in result.per_file_schemas] == [schema_a, schema_b]


def test_read_empty_paths_raises():
    with pytest.raises(ValueError, match="read called with no paths"):
        _DictRowsParser({}).read([])


def test_read_accepts_polars_lazyframe_from_parse_file(tmp_path):
    """The fast path: parser returns a LazyFrame; framework still attaches
    tracking columns and forces String dtype on the data columns."""
    lf = pl.DataFrame({"id": ["1", "2"], "name": ["alpha", "beta"]}).lazy()
    parser = _DictRowsParser(rows_per_path={"a.tst": []})
    parser.parse_file = lambda path, *, table_name_hint=None: ParsedFile(
        rows=lf, schema=ParserSchema(column_names=["id", "name"]),
    )
    df = parser.read([_touch(tmp_path, "a.tst")]).frame.collect()
    assert df.schema["id"] == pl.String
    assert df.schema["name"] == pl.String
    assert df["__source_file__"].to_list() == ["a.tst"] * 2
    assert df["__row_index__"].to_list() == [1, 2]
