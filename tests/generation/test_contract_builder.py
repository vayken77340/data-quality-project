from pathlib import Path

import pytest

from data_contract.generation.config import ColumnMapping, KeysSpec, MergedConfig, TableSelector
from dq_core.contract import Contract, Rejection
from data_contract.generation.builder import build_contract, write_outputs
from data_contract.generation.spec_reader import RawField, SheetSpec
from dq_core.type_mapping import load_type_registry


def _mapping():
    return ColumnMapping.from_dict({
        "extract_name": {"spec_name": "Champ dans extract"},
        "type": {"spec_name": "Type"},
        "description": {"spec_name": "Description", "default_value": None},
        "table": {"spec_name": "Table", "default_value": None},
        "nullable": {
            "spec_name": "Obligatoire",
            "values": {"true": ["non"], "false": ["oui"]},
        },
    })


def _keys_spec():
    return KeysSpec.from_dict({
        "sheet_name": "Keys",
        "column_mapping": {
            "table_name":  {"spec_name": "Table"},
            "primary_key": {"spec_name": "PK", "separator": "|"},
        },
    })


def _merged(mapping=None, version="1.0"):
    return MergedConfig(
        epic="1118",
        version=version,
        spec_file_name="ignored.xlsx",
        target="postgres",
        tables=[TableSelector(table_name="PROJECT")],
        column_mapping=mapping or _mapping(),
        keys=_keys_spec(),
        epic_config_path=Path("dummy.yaml"),
    )


@pytest.fixture(scope="module")
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def _sheet_spec(name="PROJECT", *, has_table_column=False):
    return SheetSpec(
        sheet_name=name,
        header_row=1,
        col_idx={"extract_name": 0, "type": 1, "description": 2, "nullable": 3},
        has_table_column=has_table_column,
    )


def _row(sheet_row, name="x", type_="Double", desc="d", nullable="OUI", table=None):
    return RawField(sheet_row=sheet_row, extract_raw=name, type_raw=type_, description_raw=desc, nullable_raw=nullable, table_raw=table)


def test_happy_path(registry):
    rows = [
        _row(2, "proj_id", "Double", "Identifier", "OUI"),
        _row(3, "label", "VARCHAR(50)", "Label", "NON"),
    ]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
        now="2026-06-02T14:00:00Z",
    )
    assert isinstance(result, Contract)
    assert result.table == "PROJECT"
    assert result.version == "1.0"
    assert result.fields[0].name == "proj_id"
    assert result.fields[0].nullable is False  # OUI = value_required = not nullable
    assert result.fields[1].nullable is True   # NON = optional = nullable
    assert result.fields[1].max_length == 50
    assert result.spec_file == "x.xlsx"
    assert result.spec_sheet == "PROJECT"
    assert result.generated_at == "2026-06-02T14:00:00Z"


def test_table_value_from_table_column(registry):
    rows = [
        _row(2, "id", "Double", "d", "OUI", table="ACTUAL_TABLE"),
        _row(3, "x",  "Double", "d", "OUI", table="ACTUAL_TABLE"),
    ]
    result = build_contract(
        _merged(),
        _sheet_spec(has_table_column=True),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    assert isinstance(result, Contract)
    assert result.table == "ACTUAL_TABLE"
    assert result.spec_sheet == "PROJECT"  # but the sheet name stays in source


def test_table_value_defaults_to_sheet_name(registry):
    rows = [_row(2, "id", "Double", "d", "OUI")]
    result = build_contract(
        _merged(),
        _sheet_spec(has_table_column=False),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    assert isinstance(result, Contract)
    assert result.table == "PROJECT"


def test_duplicate_field_name_rejects(registry):
    rows = [
        _row(2, "dup", "Double", "d", "OUI"),
        _row(3, "dup", "Double", "d", "OUI"),
    ]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    assert isinstance(result, Rejection)
    assert any(e.kind == "duplicate_field" for e in result.errors)


def test_multi_table_in_sheet(registry):
    rows = [
        _row(2, "a", "Double", "d", "OUI", table="A"),
        _row(3, "b", "Double", "d", "OUI", table="B"),
    ]
    result = build_contract(
        _merged(),
        _sheet_spec(has_table_column=True),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    assert isinstance(result, Rejection)
    assert any(e.kind == "multi_table_in_sheet" for e in result.errors)


def test_write_outputs_success_creates_history_and_deletes_rejected(tmp_path: Path, registry):
    contracts_dir = tmp_path / "contracts"
    (contracts_dir / "rejected").mkdir(parents=True)
    stale = contracts_dir / "rejected" / "PROJECT.yaml"
    stale.write_text("stale\n", encoding="utf-8")

    rows = [_row(2, "id", "Double", "d", "OUI")]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    paths = write_outputs(result, contracts_dir)
    canonical = contracts_dir / "PROJECT.yaml"
    history = contracts_dir / "history" / "1.0" / "PROJECT.yaml"
    assert canonical in paths
    assert history in paths
    assert canonical.exists()
    assert history.exists()
    assert canonical.read_text(encoding="utf-8") == history.read_text(encoding="utf-8")
    assert not stale.exists()


def test_write_outputs_rejection_deletes_canonical_and_keeps_history(tmp_path: Path, registry):
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir(parents=True)
    canonical = contracts_dir / "PROJECT.yaml"
    canonical.write_text("stale-good\n", encoding="utf-8")
    history_dir = contracts_dir / "history" / "0.9"
    history_dir.mkdir(parents=True)
    history_old = history_dir / "PROJECT.yaml"
    history_old.write_text("dont-touch\n", encoding="utf-8")

    rows = [_row(2, "dup", "Double", "d", "OUI"), _row(3, "dup", "Double", "d", "OUI")]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    paths = write_outputs(result, contracts_dir)
    rejected = contracts_dir / "rejected" / "PROJECT.yaml"
    assert paths == [rejected]
    assert rejected.exists()
    assert not canonical.exists()
    # history is untouched
    assert history_old.exists()
    assert history_old.read_text(encoding="utf-8") == "dont-touch\n"
