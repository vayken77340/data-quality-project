from pathlib import Path

import pytest

from data_contract.generation.config import ColumnMapping, KeysSpec, MergedConfig, TableSelector
from data_contract.contract import Contract, Rejection
from data_contract.generation.builder import build_contract
from data_contract.generation.spec_reader import RawField, SheetSpec
from data_contract.type_mapping import load_type_registry


def _mapping():
    return ColumnMapping.from_dict({
        "name": {"spec_name": "Champ dans extract"},
        "type": {"spec_name": "Type"},
        "description": {"spec_name": "Description", "default_value": None},
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


def _merged():
    return MergedConfig(
        epic="1118",
        version="1.0",
        spec_file_name="x.xlsx",
        target="postgres",
        tables=[TableSelector(table_name="PROJECT")],
        column_mapping=_mapping(),
        keys=_keys_spec(),
        epic_config_path=Path("dummy.yaml"),
    )


def _sheet_spec():
    return SheetSpec(
        sheet_name="PROJECT",
        header_row=1,
        col_idx={"name": 0, "type": 1, "description": 2, "nullable": 3},
        has_table_column=False,
    )


@pytest.fixture(scope="module")
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_multiple_errors_collected_not_first_only(registry):
    rows = [
        RawField(2, "good", "Double", "desc", "OUI", None),
        RawField(3, "x", "WhoKnows", "desc", "OUI", None),       # unknown_type
        RawField(4, "y", "Double", "desc", "maybe", None),        # invalid_nullable
        RawField(5, None, "Double", "desc", "OUI", None),         # missing_mandatory name
    ]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
    )
    assert isinstance(result, Rejection)
    kinds = sorted(e.kind for e in result.errors)
    assert "unknown_type" in kinds
    assert "invalid_nullable" in kinds
    assert "missing_mandatory" in kinds
    assert len(result.errors) >= 3


def test_allow_unknown_types_downgrades(registry):
    rows = [RawField(2, "x", "WhoKnows", "desc", "OUI", None)]
    result = build_contract(
        _merged(),
        _sheet_spec(),
        rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
        allow_unknown_types=True,
    )
    assert isinstance(result, Contract)
    assert result.fields[0].type.value == "unknown"
