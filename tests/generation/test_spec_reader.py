from pathlib import Path

from openpyxl import Workbook

from data_contract.generation.config import ColumnMapping
from data_contract.generation.spec_reader import (
    iter_field_rows,
    list_table_spec_sheets,
    open_workbook,
    read_sheet,
)


COLUMN_MAPPING_RAW = {
    "extract_name": {"spec_name": "Champ dans extract"},
    "type": {"spec_name": "Type"},
    "description": {"spec_name": "Description", "default_value": None},
    "nullable": {
        "spec_name": "Obligatoire",
        "values": {"true": ["non"], "false": ["oui"]},
    },
}


def _mapping():
    return ColumnMapping.from_dict(COLUMN_MAPPING_RAW)


def test_list_table_spec_sheets_skips_non_spec_sheets(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        sheets = list_table_spec_sheets(wb, _mapping())
    finally:
        wb.close()
    assert sheets == ["WIDGETS"]


def test_read_sheet_locates_header_on_row_3(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        result = read_sheet(wb, "WIDGETS", _mapping())
    finally:
        wb.close()
    assert result.error is None
    spec = result.spec
    assert spec is not None
    assert spec.header_row == 3
    assert spec.col_idx["extract_name"] == 0
    assert spec.col_idx["nullable"] == 3
    assert spec.has_table_column is False


def test_iter_field_rows_skips_blank_trailing_row(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        spec = read_sheet(wb, "WIDGETS", _mapping()).spec
        rows = list(iter_field_rows(wb, spec))
    finally:
        wb.close()
    assert [r.extract_raw for r in rows] == ["widget_id", "label"]


def test_iter_field_rows_reads_silver_and_bronze_columns(tmp_path: Path):
    """When the workbook has Nom BDD and Nom Bronze columns, the reader
    surfaces them on every RawField. Step 3 will plumb these into the
    builder; for now the reader's job is just to expose them."""
    path = tmp_path / "with_silver_bronze.xlsx"
    wb = Workbook()
    spec = wb.active
    spec.title = "WIDGETS"
    spec.append(["Champ dans extract", "Nom BDD", "Nom Bronze", "Type", "Description", "Obligatoire"])
    spec.append(["Record Number", "record_number", "record no", "Double", "id", "non"])
    spec.append(["Plain Field", None, None, "VARCHAR(50)", "plain", "oui"])
    wb.save(path)

    mapping = ColumnMapping.from_dict({
        "extract_name": {"spec_name": "Champ dans extract"},
        "silver_name":  {"spec_name": "Nom BDD", "column_required": False, "default_value": None},
        "bronze_name":  {"spec_name": "Nom Bronze", "column_required": False, "default_value": None},
        "type": {"spec_name": "Type"},
        "description": {"spec_name": "Description", "default_value": None},
        "nullable": {
            "spec_name": "Obligatoire",
            "values": {"true": ["non"], "false": ["oui"]},
        },
    })

    workbook = open_workbook(path)
    try:
        sheet_spec = read_sheet(workbook, "WIDGETS", mapping).spec
        assert sheet_spec is not None
        assert "silver_name" in sheet_spec.col_idx
        assert "bronze_name" in sheet_spec.col_idx
        rows = list(iter_field_rows(workbook, sheet_spec))
    finally:
        workbook.close()

    assert [r.extract_raw for r in rows] == ["Record Number", "Plain Field"]
    assert [r.silver_raw for r in rows] == ["record_number", None]
    assert [r.bronze_raw for r in rows] == ["record no", None]


def test_iter_field_rows_skips_row_with_only_stray_non_identity_cell(tmp_path: Path):
    """Trailing rows with junk in non-identity columns (Table override, a
    stray Obligatoire dropdown default, a leftover Description) but blank
    extract_name are treated as empty and skipped -- not flagged as
    missing_mandatory extract_name."""
    path = tmp_path / "trailing_junk.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "WIDGETS"
    ws.append(["Champ dans extract", "Type", "Description", "Obligatoire"])
    ws.append(["widget_id", "Double", "id", "non"])
    # Trailing row: extract_name blank, but Obligatoire has a leftover "oui"
    # (very common with dropdown-validation defaults extending down the column).
    ws.append([None, None, None, "oui"])
    # Another trailing row with only Description stray content.
    ws.append([None, None, "leftover description", None])
    wb.save(path)

    workbook = open_workbook(path)
    try:
        sheet_spec = read_sheet(workbook, "WIDGETS", _mapping()).spec
        rows = list(iter_field_rows(workbook, sheet_spec))
    finally:
        workbook.close()

    assert [r.extract_raw for r in rows] == ["widget_id"]


def test_read_sheet_missing_sheet_returns_error(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        result = read_sheet(wb, "DOES_NOT_EXIST", _mapping())
    finally:
        wb.close()
    assert result.spec is None
    assert result.error is not None
    assert result.error.kind == "header_not_found"
