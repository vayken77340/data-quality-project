"""Direct tests for `builder.build_one_table` -- the per-table core shared
by `process_epic` and `backfill_missing_history`."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from dq_core.contract import Contract, Rejection
from data_contract.generation.builder import build_one_table
from data_contract.generation.config import (
    ColumnMapping, KeysSpec, MergedConfig, TableSelector,
)
from data_contract.generation.keys import build_pk_index, read_keys_sheet
from data_contract.generation.spec_reader import open_workbook
from dq_core.settings import Settings
from dq_core.type_mapping import load_type_registry


def _mapping() -> ColumnMapping:
    return ColumnMapping.from_dict({
        "name": {"spec_name": "Champ dans extract"},
        "type": {"spec_name": "Type"},
        "description": {"spec_name": "Description", "default_value": None},
        "nullable": {
            "spec_name": "Obligatoire",
            "values": {"true": ["non"], "false": ["oui"]},
        },
    })


def _keys_spec() -> KeysSpec:
    return KeysSpec.from_dict({
        "sheet_name": "Keys",
        "column_mapping": {
            "table_name":  {"spec_name": "Table"},
            "primary_key": {"spec_name": "PK", "separator": "|"},
        },
    })


def _merged() -> MergedConfig:
    return MergedConfig(
        epic="E", version="1.0", spec_file_name="ignored.xlsx",
        target="postgres",
        tables=[TableSelector(table_name="T")],
        column_mapping=_mapping(),
        keys=_keys_spec(),
        epic_config_path=Path("dummy.yaml"),
    )


def _write_workbook(path: Path, *, include_t_sheet: bool = True) -> None:
    wb = Workbook()
    wb.active.title = "Keys"
    keys = wb["Keys"]
    keys.append(["Table", "PK"])
    keys.append(["T", "id"])
    if include_t_sheet:
        spec = wb.create_sheet("T")
        spec.append(["Champ dans extract", "Type", "Description", "Obligatoire"])
        spec.append(["id", "INTEGER", "row id", "oui"])
        spec.append(["name", "VARCHAR(50)", "row name", "non"])
    wb.save(path)


@pytest.fixture
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_happy_path_returns_contract(tmp_path: Path, registry):
    """A well-formed sheet yields a Contract with the declared fields and
    primary-key enrichment from the Keys sheet."""
    path = tmp_path / "spec.xlsx"
    _write_workbook(path)
    wb = open_workbook(path)
    try:
        merged = _merged()
        keys_data = read_keys_sheet(wb, merged.keys)
        result = build_one_table(
            merged, TableSelector(table_name="T"), wb,
            registry=registry,
            spec_file_rel="spec.xlsx",
            allow_unknown_types=False,
            keys_data=keys_data,
            pk_index=build_pk_index(keys_data.rows),
            seen_tables={},
            settings=Settings(),
        )
    finally:
        wb.close()

    assert isinstance(result, Contract)
    assert result.table == "T"
    assert [f.name for f in result.fields] == ["id", "name"]
    id_field = next(f for f in result.fields if f.name == "id")
    assert id_field.primary_key is True


def test_missing_sheet_returns_rejection_with_read_error(tmp_path: Path, registry):
    """When `read_sheet` reports an error, the helper short-circuits to a
    Rejection that includes the read error (plus any keys_data errors)."""
    path = tmp_path / "spec.xlsx"
    _write_workbook(path, include_t_sheet=False)
    wb = open_workbook(path)
    try:
        merged = _merged()
        keys_data = read_keys_sheet(wb, merged.keys)
        result = build_one_table(
            merged, TableSelector(table_name="T"), wb,
            registry=registry,
            spec_file_rel="spec.xlsx",
            allow_unknown_types=False,
            keys_data=keys_data,
            pk_index=build_pk_index(keys_data.rows),
            seen_tables={},
            settings=Settings(),
        )
    finally:
        wb.close()

    assert isinstance(result, Rejection)
    # The Rejection includes the header_not_found read error.
    kinds = [e.kind for e in result.errors]
    assert "header_not_found" in kinds


def test_duplicate_sheet_routed_through_check_duplicate_table(tmp_path: Path, registry):
    """Calling the helper twice with the same `seen_tables` dict should make
    the second call produce a `duplicate_table_across_sheets` rejection on a
    disambiguated table name."""
    path = tmp_path / "spec.xlsx"
    _write_workbook(path)
    wb = open_workbook(path)
    try:
        merged = _merged()
        keys_data = read_keys_sheet(wb, merged.keys)
        seen_tables: dict[str, str] = {}
        first = build_one_table(
            merged, TableSelector(table_name="T"), wb,
            registry=registry, spec_file_rel="spec.xlsx",
            allow_unknown_types=False, keys_data=keys_data,
            pk_index=build_pk_index(keys_data.rows),
            seen_tables=seen_tables, settings=Settings(),
        )
        # Re-invoke with the same `seen_tables` to simulate a second sheet
        # resolving to the same table name.
        second = build_one_table(
            merged, TableSelector(table_name="T"), wb,
            registry=registry, spec_file_rel="spec.xlsx",
            allow_unknown_types=False, keys_data=keys_data,
            pk_index=build_pk_index(keys_data.rows),
            seen_tables=seen_tables, settings=Settings(),
        )
    finally:
        wb.close()

    assert isinstance(first, Contract)
    assert isinstance(second, Rejection)
    assert any(e.kind == "duplicate_table_across_sheets" for e in second.errors)
