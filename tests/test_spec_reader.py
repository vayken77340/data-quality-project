from pathlib import Path

from data_quality.config import ColumnMapping
from data_quality.spec_reader import (
    iter_field_rows,
    list_table_spec_sheets,
    open_workbook,
    read_sheet,
)


COLUMN_MAPPING_RAW = {
    "name": {"spec_name": "Champ dans extract", "mandatory": True},
    "type": {"spec_name": "Type", "mandatory": True},
    "description": {"spec_name": "Description", "mandatory": False},
    "nullable": {
        "spec_name": "Obligatoire",
        "mandatory": True,
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
    assert spec.col_idx["name"] == 0
    assert spec.col_idx["nullable"] == 3
    assert spec.has_table_column is False


def test_iter_field_rows_skips_blank_trailing_row(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        spec = read_sheet(wb, "WIDGETS", _mapping()).spec
        rows = list(iter_field_rows(wb, spec))
    finally:
        wb.close()
    assert [r.name_raw for r in rows] == ["widget_id", "label"]


def test_read_sheet_missing_sheet_returns_error(tiny_spec: Path):
    wb = open_workbook(tiny_spec)
    try:
        result = read_sheet(wb, "DOES_NOT_EXIST", _mapping())
    finally:
        wb.close()
    assert result.spec is None
    assert result.error is not None
    assert result.error.kind == "header_not_found"
