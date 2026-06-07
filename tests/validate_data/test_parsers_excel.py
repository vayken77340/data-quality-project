from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from data_contract.validate_data.parsers.excel import ExcelParser


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


def test_excel_single_sheet_row_count(tmp_path):
    p = _write_xlsx(tmp_path / "a.xlsx", [["id", "name"], [1, "alpha"], [2, "beta"]])
    parser = ExcelParser()
    df = parser.read([p]).collect()
    assert df.height == 2
    assert set(df.columns) >= {"id", "name", "__source_file__", "__row_index__"}
    assert df["__source_file__"].to_list() == ["a.xlsx", "a.xlsx"]


def test_excel_multi_sheet_workbook_reads_only_first_sheet(tmp_path):
    """Per design: secondary sheets are intentionally ignored."""
    p = _write_xlsx(
        tmp_path / "multi.xlsx",
        [["id", "name"], [1, "alpha"]],
        extra_sheets={
            "Other": [["x", "y"], [99, "ignored"]],
        },
    )
    parser = ExcelParser()
    df = parser.read([p]).collect()
    assert df.height == 1
    # Confirm the "Other" sheet's data did NOT bleed in.
    assert 99 not in df["id"].to_list()


def test_excel_header_row_offset(tmp_path):
    p = _write_xlsx(
        tmp_path / "h.xlsx",
        [
            ["preface", None, None],
            [None, None, None],
            ["id", "name", "amount"],
            [1, "alpha", 10],
        ],
    )
    parser = ExcelParser({"header_row": 3})
    df = parser.read([p]).collect()
    assert df.height == 1
    assert set(df.columns) >= {"id", "name", "amount"}
