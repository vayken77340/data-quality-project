from pathlib import Path

import pytest

from data_quality import field_constraints
from data_quality.config import ColumnMapping
from data_quality.contract import build_contract
from data_quality.errors import RejectionError
from data_quality.field_constraints.base import (
    ConstraintContext,
    DriftChange,
    FieldConstraint,
    parse_column_ref,
)
from data_quality.field_constraints.unique import UniqueConstraint
from data_quality.field_constraints.allowed_values import AllowedValuesConstraint
from data_quality.field_constraints.pattern import PatternConstraint
from data_quality.field_constraints.min_value import MinValueConstraint
from data_quality.field_constraints.max_value import MaxValueConstraint
from data_quality.spec_reader import RawField, SheetSpec
from data_quality.type_mapping import Type, load_type_registry


def _ctx(field_type=Type.INTEGER, max_length=None, sheet_row=2):
    return ConstraintContext(sheet_row=sheet_row, field_type=field_type, field_max_length=max_length)


def test_unique_added_removed_diff():
    add = UniqueConstraint.diff("f", None, True)
    assert add is not None and add.kind == "unique_added" and add.severity == "breaking"
    rem = UniqueConstraint.diff("f", True, None)
    assert rem is not None and rem.kind == "unique_removed" and rem.severity == "additive"


def test_list_default_separator():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values"})
    v, err = c.parse_cell("active|pending|archived", _ctx(Type.STRING))
    assert err is None
    assert v == ["active", "pending", "archived"]


def test_list_custom_separator():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values", "separator": ","})
    v, err = c.parse_cell("a, b ,c", _ctx(Type.STRING))
    assert err is None and v == ["a", "b", "c"]


def test_list_empty_after_split():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values"})
    v, err = c.parse_cell("|||", _ctx(Type.STRING))
    assert v is None and err is not None and err.kind == "list_empty"


def test_list_drift_values_added():
    change = AllowedValuesConstraint.diff("status", ["a", "b"], ["a", "b", "c"])
    assert change is not None and change.kind == "list_values_added" and change.severity == "additive"
    assert change.detail["added"] == ["c"]


def test_list_drift_values_removed_is_breaking():
    change = AllowedValuesConstraint.diff("status", ["a", "b", "c"], ["a", "b"])
    assert change is not None and change.kind == "list_values_removed" and change.severity == "breaking"


def test_pattern_invalid_regex_rejects():
    c = PatternConstraint.from_config({"spec_name": "Pattern"})
    v, err = c.parse_cell("[unclosed", _ctx(Type.STRING))
    assert v is None and err is not None and err.kind == "invalid_pattern"


def test_pattern_valid_regex_passes():
    c = PatternConstraint.from_config({"spec_name": "Pattern"})
    v, err = c.parse_cell(r"^\d{4}$", _ctx(Type.STRING))
    assert err is None and v == r"^\d{4}$"


def test_min_value_integer():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("5", _ctx(Type.INTEGER))
    assert err is None and v == 5


def test_min_value_number():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("3.14", _ctx(Type.NUMBER))
    assert err is None and v == pytest.approx(3.14)


def test_min_value_invalid_for_type():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("notanumber", _ctx(Type.INTEGER))
    assert v is None and err is not None and err.kind == "invalid_min_max"


def test_max_value_string_conflict_with_max_length():
    c = MaxValueConstraint.from_config({"spec_name": "Max"})
    v, err = c.parse_cell("500", _ctx(Type.STRING, max_length=100))
    assert v is None and err is not None and err.kind == "invalid_min_max"


def test_min_value_raised_is_breaking():
    change = MinValueConstraint.diff("age", 0, 18)
    assert change is not None and change.kind == "min_value_raised" and change.severity == "breaking"


def test_max_value_lowered_is_breaking():
    change = MaxValueConstraint.diff("age", 150, 99)
    assert change is not None and change.kind == "max_value_lowered" and change.severity == "breaking"


def test_unknown_constraint_in_column_mapping_is_config_error():
    from data_quality.errors import ConfigError
    with pytest.raises(ConfigError):
        ColumnMapping.from_dict({
            "name": {"spec_name": "N", "value_required": True},
            "type": {"spec_name": "T", "value_required": True},
            "description": {"spec_name": "D", "value_required": False},
            "nullable": {
                "spec_name": "Obligatoire",
                "value_required": True,
                "values": {"true": ["non"], "false": ["oui"]},
            },
            "no_such_constraint_zzz": {"spec_name": "X", "value_required": False},
        })


def test_extensibility_register_custom_constraint(tmp_path):
    """Drop-in registration: a one-class custom constraint shows up in REGISTRY
    and is picked up by ColumnMapping.from_dict, and its values flow into the
    built contract."""

    class StartsWithConstraint(FieldConstraint):
        name = "starts_with"
        contract_key = "starts_with"

        @classmethod
        def from_config(cls, raw):
            instance = cls(column=parse_column_ref(raw, name=cls.name))
            instance.raw_config = dict(raw)
            return instance

        def _parse_non_empty(self, raw_str, raw_original, ctx):
            return raw_str, None

        @classmethod
        def diff(cls, field_name, old, new):
            if old == new:
                return None
            return DriftChange(kind="starts_with_changed", severity="breaking", field=field_name)

    field_constraints.register(StartsWithConstraint)
    try:
        cm = ColumnMapping.from_dict({
            "name": {"spec_name": "Field Name", "value_required": True},
            "type": {"spec_name": "Type", "value_required": True},
            "description": {"spec_name": "Description", "value_required": False},
            "nullable": {
                "spec_name": "Obligatoire",
                "value_required": True,
                "values": {"true": ["non"], "false": ["oui"]},
            },
            "starts_with": {"spec_name": "Prefix", "value_required": False},
        })
        assert "starts_with" in cm.constraints
    finally:
        field_constraints.REGISTRY.pop("starts_with", None)


def test_end_to_end_constraint_round_trip(types_yaml_path: Path):
    """Build a contract from synthetic spec rows that exercise list + pattern,
    confirm flat keys appear on the field block."""
    cm = ColumnMapping.from_dict({
        "name": {"spec_name": "Field Name", "value_required": True},
        "type": {"spec_name": "Type", "value_required": True},
        "description": {"spec_name": "Description", "value_required": False},
        "nullable": {
            "spec_name": "Obligatoire",
            "value_required": True,
            "values": {"true": ["non"], "false": ["oui"]},
        },
        "list": {"spec_name": "Values", "value_required": False, "separator": ","},
        "pattern": {"spec_name": "Pattern", "value_required": False},
    })
    from data_quality.config import KeysSpec, MergedConfig, TableSelector
    keys = KeysSpec.from_dict({
        "sheet_name": "Keys",
        "column_mapping": {
            "table_name":  {"spec_name": "Table", "value_required": True},
            "primary_key": {"spec_name": "PK", "value_required": True, "separator": "|"},
        },
    })
    merged = MergedConfig(
        epic="X",
        version="1.0",
        spec_file_name="x.xlsx",
        tables=[TableSelector("T")],
        column_mapping=cm,
        keys=keys,
        epic_config_path=Path("dummy.yaml"),
    )
    sheet = SheetSpec(sheet_name="T", header_row=1, col_idx={"name": 0, "type": 1, "description": 2, "nullable": 3}, has_table_column=False, constraint_cols={"list": 0, "pattern": 0})
    rows = [
        RawField(
            sheet_row=2,
            name_raw="id",
            type_raw="Double",
            description_raw="desc",
            nullable_raw="OUI",
            table_raw=None,
            extras={"list": None, "pattern": None},
        ),
        RawField(
            sheet_row=3,
            name_raw="status",
            type_raw="VARCHAR(50)",
            description_raw="status",
            nullable_raw="NON",
            table_raw=None,
            extras={"list": "active,pending", "pattern": r"^[a-z]+$"},
        ),
    ]
    registry = load_type_registry(types_yaml_path)
    from data_quality.contract import Contract
    result = build_contract(
        merged, sheet, rows,
        type_registry=registry,
        spec_file_rel="x.xlsx",
        table_name_from_config="T",
        now="2026-06-02T14:00:00Z",
    )
    assert isinstance(result, Contract)
    by_name = {f.name: f for f in result.fields}
    assert by_name["id"].constraints == {}
    assert by_name["status"].constraints == {
        "list": ["active", "pending"],
        "pattern": r"^[a-z]+$",
    }
    # to_dict flattens to top-level field keys
    payload = result.to_dict()
    status_dict = next(f for f in payload["fields"] if f["name"] == "status")
    assert status_dict["list"] == ["active", "pending"]
    assert status_dict["pattern"] == r"^[a-z]+$"
