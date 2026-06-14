from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
from openpyxl import Workbook

from data_contract.data_parsers.excel import ExcelParser, _to_string


def _write_xlsx(path: Path, rows: list[list], *, sheet_name: str = "Sheet1", extra_sheets=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    for name, sheet_rows in (extra_sheets or {}).items():
        s = wb.create_sheet(name)
        for row in sheet_rows:
            s.append(row)
    wb.save(path)
    return path


def test_excel_single_sheet_row_count(tmp_path, excel_params):
    p = _write_xlsx(tmp_path / "a.xlsx", [["id", "name"], [1, "alpha"], [2, "beta"]])
    parser = ExcelParser(excel_params())
    df = parser.read([p]).frame.collect()
    assert df.height == 2
    assert set(df.columns) >= {"id", "name", "__source_file__", "__row_index__"}
    assert df["__source_file__"].to_list() == ["a.xlsx", "a.xlsx"]


def test_excel_multi_sheet_workbook_reads_only_first_sheet(tmp_path, excel_params):
    """Per design: secondary sheets are intentionally ignored."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id", "name"], [1, "alpha"]],
        extra_sheets={
            "Other": [["x", "y"], [99, "ignored"]],
        },
    )
    parser = ExcelParser(excel_params())
    df = parser.read([p]).frame.collect()
    assert df.height == 1
    # Confirm the "Other" sheet's data did NOT bleed in.
    assert "99" not in df["id"].to_list()


def test_excel_sheet_name_selects_named_sheet(tmp_path, excel_params):
    """When `sheet_name` is set, the parser reads that sheet instead of the first."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id", "name"], [1, "from-first"]],
        sheet_name="First",
        extra_sheets={
            "Project Data": [["id", "name"], [99, "from-project"], [100, "second"]],
        },
    )
    parser = ExcelParser(excel_params(**{"sheet_name": "Project Data"}))
    df = parser.read([p]).frame.collect()
    assert df.height == 2
    # Cells are stringified at the parser; type validation happens in the
    # contract layer.
    assert df["id"].to_list() == ["99", "100"]


def test_excel_unset_sheet_name_falls_back_to_first(tmp_path, excel_params):
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id"], [1]],
        sheet_name="First",
        extra_sheets={"Other": [["id"], [99]]},
    )
    parser = ExcelParser(excel_params())
    df = parser.read([p]).frame.collect()
    assert df["id"].to_list() == ["1"]


def test_excel_table_name_hint_used_when_no_explicit_sheet_name(tmp_path, excel_params):
    """When sheet_name is unset and a sheet matches the table_name_hint, it's used."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id"], [1]],
        sheet_name="Other",
        extra_sheets={
            "PROJECT": [["id"], [42], [43]],
        },
    )
    parser = ExcelParser(excel_params())  # no sheet_name configured
    df = parser.read([p], table_name_hint="PROJECT").frame.collect()
    assert df["id"].to_list() == ["42", "43"]


def test_excel_table_name_hint_ignored_when_no_matching_sheet(tmp_path, excel_params):
    """When no sheet matches the hint, falls back to the first sheet."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id"], [1]],
        sheet_name="First",
        extra_sheets={"Other": [["id"], [99]]},
    )
    parser = ExcelParser(excel_params())
    df = parser.read([p], table_name_hint="NONEXISTENT").frame.collect()
    # Falls back to first sheet.
    assert df["id"].to_list() == ["1"]


def test_excel_explicit_sheet_name_beats_table_name_hint(tmp_path, excel_params):
    """An explicit sheet_name in params wins over the runner's table_name_hint."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id"], [1]],
        sheet_name="First",
        extra_sheets={
            "PROJECT": [["id"], [42]],
            "CALENDAR": [["id"], [99]],
        },
    )
    parser = ExcelParser(excel_params(**{"sheet_name": "CALENDAR"}))
    df = parser.read([p], table_name_hint="PROJECT").frame.collect()
    # Explicit CALENDAR wins.
    assert df["id"].to_list() == ["99"]


def test_excel_header_row_offset(tmp_path, excel_params):
    p = _write_xlsx(
        tmp_path / "h.xlsx",
        [
            ["preface", None, None],
            [None, None, None],
            ["id", "name", "amount"],
            [1, "alpha", 10],
        ],
    )
    parser = ExcelParser(excel_params(**{"header_row": 3}))
    df = parser.read([p]).frame.collect()
    assert df.height == 1
    assert set(df.columns) >= {"id", "name", "amount"}


# ---------------------------------------------------------------------------
# New: every-cell-as-string contract
# ---------------------------------------------------------------------------


def test_excel_all_data_columns_are_string_dtype(tmp_path, excel_params):
    """Mirror of the CSV invariant: every data column reads as pl.String."""
    p = _write_xlsx(
        tmp_path / "mixed.xlsx",
        [
            ["i", "f", "d", "b", "s"],
            [1, 1.5, date(2024, 1, 15), True, "abc"],
            [2, 3.14, date(2024, 2, 1), False, "xyz"],
        ],
    )
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    for col in ("i", "f", "d", "b", "s"):
        assert df.schema[col] == pl.String, f"{col!r} dtype is {df.schema[col]!r}"


def test_excel_whole_number_cell_stringifies_without_trailing_zero(tmp_path, excel_params):
    """xlsx stores all numbers as floats. The `_to_string` helper downcasts
    whole-number floats to int so the contract's INTEGER regex isn't broken
    by Excel's storage format."""
    p = _write_xlsx(tmp_path / "n.xlsx", [["x"], [42], [0], [-7]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["x"].to_list() == ["42", "0", "-7"]


def test_excel_fractional_float_stringifies_as_float(tmp_path, excel_params):
    p = _write_xlsx(tmp_path / "f.xlsx", [["x"], [1.5], [3.14]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["x"].to_list() == ["1.5", "3.14"]


def test_excel_date_cell_stringifies_to_iso(tmp_path, excel_params):
    """Python's `str(date)` is ISO-form, which matches the default DATE
    parse_format `%Y-%m-%d` in configs/types.yaml."""
    p = _write_xlsx(tmp_path / "d.xlsx", [["d"], [date(2024, 1, 15)]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["d"].to_list() == ["2024-01-15"]


def test_excel_datetime_cell_stringifies_with_space_separator(tmp_path, excel_params):
    """Python's `str(datetime)` uses a space separator, not T. The default
    TIMESTAMP parse_formats list has the space form first to match."""
    p = _write_xlsx(tmp_path / "t.xlsx", [["t"], [datetime(2024, 1, 15, 10, 30, 0)]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["t"].to_list() == ["2024-01-15 10:30:00"]


def test_excel_bool_cell_stringifies_to_python_default(tmp_path, excel_params):
    """`str(True)` is `'True'` (capital T). The BOOLEAN data_values block
    lowercases tokens at load time, so `True` -> `true` matches `true` in the
    token list."""
    p = _write_xlsx(tmp_path / "b.xlsx", [["b"], [True], [False]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["b"].to_list() == ["True", "False"]


def test_excel_text_formatted_cell_passes_through_verbatim(tmp_path, excel_params):
    """A cell author-typed as text (apostrophe prefix in Excel, or Format
    Cells -> Text) preserves leading zeros. Calamine reads it back as a
    string and `_to_string` is identity for strings."""
    p = _write_xlsx(tmp_path / "z.xlsx", [["code"], ["00042"], ["00007"]])
    df = ExcelParser(excel_params()).read([p]).frame.collect()
    assert df["code"].to_list() == ["00042", "00007"]


def test_excel_null_tokens_mapping(tmp_path, excel_params):
    # Trailing empty rows are stripped by openpyxl; the final non-empty row
    # keeps the empty-cell row in place so we can test the null-token map.
    p = _write_xlsx(
        tmp_path / "n.xlsx",
        [["v"], ["NULL"], ["present"], [""], ["tail"]],
    )
    df = ExcelParser(excel_params(**{"null_tokens": ["", "NULL"]})).read([p]).frame.collect()
    assert df["v"].to_list() == [None, "present", None, "tail"]


# ---------------------------------------------------------------------------
# _to_string helper unit tests
# ---------------------------------------------------------------------------


def test_to_string_none():
    assert _to_string(None) is None


def test_to_string_int():
    assert _to_string(42) == "42"


def test_to_string_whole_float_downcasts():
    assert _to_string(42.0) == "42"


def test_to_string_fractional_float():
    assert _to_string(3.14) == "3.14"


def test_to_string_bool():
    assert _to_string(True) == "True"
    assert _to_string(False) == "False"


def test_to_string_str_identity():
    assert _to_string("00042") == "00042"


def test_to_string_date():
    assert _to_string(date(2024, 1, 15)) == "2024-01-15"


def test_to_string_datetime():
    assert _to_string(datetime(2024, 1, 15, 10, 30, 0)) == "2024-01-15 10:30:00"
