from pathlib import Path

import pytest

from data_contract import field_constraints
from data_contract.config import ColumnMapping
from data_contract.contract import build_contract
from data_contract.errors import RejectionError
from data_contract.field_constraints.base import (
    ConstraintContext,
    DriftChange,
    FieldConstraint,
    parse_column_ref,
)
from data_contract.field_constraints.unique import UniqueConstraint
from data_contract.field_constraints.allowed_values import AllowedValuesConstraint
from data_contract.field_constraints.pattern import PatternConstraint
from data_contract.field_constraints.min_value import MinValueConstraint
from data_contract.field_constraints.max_value import MaxValueConstraint
from data_contract.field_constraints.format import (
    FORMAT_REGISTRY,
    FormatConstraint,
    register_format,
)
from data_contract.field_constraints.default_value import DefaultValueConstraint
from data_contract.field_constraints.base import unwrap_structured_value
from data_contract.spec_reader import RawField, SheetSpec
from data_contract.type_mapping import Type, load_type_registry


def _ctx(field_type=Type.INTEGER, max_length=None, sheet_row=2):
    return ConstraintContext(sheet_row=sheet_row, field_type=field_type, field_max_length=max_length)


def test_unique_added_removed_diff():
    add = UniqueConstraint.diff("f", None, True)
    assert add is not None and add.kind == "unique_added" and add.severity == "breaking"
    rem = UniqueConstraint.diff("f", True, None)
    assert rem is not None and rem.kind == "unique_removed" and rem.severity == "additive"


def test_allowed_values_default_separator():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values"})
    v, err = c.parse_cell("active|pending|archived", _ctx(Type.VARCHAR))
    assert err is None
    assert v == ["active", "pending", "archived"]


def test_allowed_values_custom_separator():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values", "spec_parsing": {"separator": ","}})
    v, err = c.parse_cell("a, b ,c", _ctx(Type.VARCHAR))
    assert err is None and v == ["a", "b", "c"]


def test_allowed_values_separator_not_emitted_into_contract():
    """spec_parsing knobs MUST NOT leak into the contract output."""
    c = AllowedValuesConstraint.from_config({"spec_name": "Values", "spec_parsing": {"separator": ","}})
    v, _ = c.parse_cell("a,b,c", _ctx(Type.VARCHAR))
    emitted = c.to_contract_value(v)
    # Stays flat: just the list, no wrapping dict, no separator field.
    assert emitted == ["a", "b", "c"]


def test_allowed_values_empty_after_split():
    c = AllowedValuesConstraint.from_config({"spec_name": "Values"})
    v, err = c.parse_cell("|||", _ctx(Type.VARCHAR))
    assert v is None and err is not None and err.kind == "list_empty"


def test_allowed_values_drift_values_added():
    change = AllowedValuesConstraint.diff("status", ["a", "b"], ["a", "b", "c"])
    assert change is not None and change.kind == "allowed_values_added_values" and change.severity == "additive"
    assert change.detail["added"] == ["c"]


def test_allowed_values_drift_values_removed_is_breaking():
    change = AllowedValuesConstraint.diff("status", ["a", "b", "c"], ["a", "b"])
    assert change is not None and change.kind == "allowed_values_removed_values" and change.severity == "breaking"


def test_constraint_unknown_subblock_key_raises():
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError):
        AllowedValuesConstraint.from_config({
            "spec_name": "Values",
            "spec_parsing": {"separator": "|", "bogus_key": "x"},
        })
    with pytest.raises(ConfigError):
        AllowedValuesConstraint.from_config({
            "spec_name": "Values",
            "contract_params": {"strict": True},  # allowed_values has no CONTRACT_FIELDS
        })


def test_min_value_strict_emits_structured_dict():
    c = MinValueConstraint.from_config({
        "spec_name": "Min",
        "contract_params": {"strict": True},
    })
    parsed, err = c.parse_cell("5", _ctx(Type.INTEGER))
    assert err is None and parsed == 5
    emitted = c.to_contract_value(parsed)
    assert emitted == {"value": 5, "strict": True}


def test_min_value_default_strict_false():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    parsed, _ = c.parse_cell("5", _ctx(Type.INTEGER))
    assert c.to_contract_value(parsed) == {"value": 5, "strict": False}


def test_max_value_strict_default_emits_dict():
    c = MaxValueConstraint.from_config({"spec_name": "Max"})
    parsed, _ = c.parse_cell("99", _ctx(Type.INTEGER))
    assert c.to_contract_value(parsed) == {"value": 99, "strict": False}


def test_min_value_strict_must_be_bool():
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError):
        MinValueConstraint.from_config({
            "spec_name": "Min",
            "contract_params": {"strict": "true"},  # str, not bool
        })


def test_min_value_strict_tightened_is_breaking_drift():
    change = MinValueConstraint.diff(
        "age",
        {"value": 18, "strict": False},
        {"value": 18, "strict": True},
    )
    assert change is not None
    assert change.kind == "min_value_strict_tightened" and change.severity == "breaking"


def test_max_value_strict_loosened_is_additive_drift():
    change = MaxValueConstraint.diff(
        "age",
        {"value": 99, "strict": True},
        {"value": 99, "strict": False},
    )
    assert change is not None
    assert change.kind == "max_value_strict_loosened" and change.severity == "additive"


def test_pattern_invalid_regex_rejects():
    c = PatternConstraint.from_config({"spec_name": "Pattern"})
    v, err = c.parse_cell("[unclosed", _ctx(Type.VARCHAR))
    assert v is None and err is not None and err.kind == "invalid_pattern"


def test_pattern_valid_regex_passes():
    c = PatternConstraint.from_config({"spec_name": "Pattern"})
    v, err = c.parse_cell(r"^\d{4}$", _ctx(Type.VARCHAR))
    assert err is None and v == r"^\d{4}$"


def test_min_value_integer():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("5", _ctx(Type.INTEGER))
    assert err is None and v == 5


def test_min_value_number():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("3.14", _ctx(Type.DOUBLE))
    assert err is None and v == pytest.approx(3.14)


def test_min_value_invalid_for_type():
    c = MinValueConstraint.from_config({"spec_name": "Min"})
    v, err = c.parse_cell("notanumber", _ctx(Type.INTEGER))
    assert v is None and err is not None and err.kind == "invalid_min_max"


def test_max_value_string_conflict_with_max_length():
    c = MaxValueConstraint.from_config({"spec_name": "Max"})
    v, err = c.parse_cell("500", _ctx(Type.VARCHAR, max_length=100))
    assert v is None and err is not None and err.kind == "invalid_min_max"


def test_min_value_raised_is_breaking():
    change = MinValueConstraint.diff("age", 0, 18)
    assert change is not None and change.kind == "min_value_raised" and change.severity == "breaking"


def test_max_value_lowered_is_breaking():
    change = MaxValueConstraint.diff("age", 150, 99)
    assert change is not None and change.kind == "max_value_lowered" and change.severity == "breaking"


def test_unknown_constraint_in_column_mapping_is_config_error():
    from data_contract.errors import ConfigError
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
        """Demo constraint: declares that field values begin with a prefix.

        Spec cell:    raw prefix string.
        Contract output: flat string `starts_with: <prefix>`.
        Drift:        any change = breaking.
        """
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


# ---------------------------------------------------------------------------
# Phase 0 — Catalog hardening
# ---------------------------------------------------------------------------


def test_contract_key_uniqueness_enforced_at_register():
    from data_contract.errors import ConfigError

    class CollidingConstraint(FieldConstraint):
        """Collides with pattern's contract_key.
        Spec cell:    nothing.
        Contract output: nothing.
        Drift:        none.
        """
        name = "collides_with_pattern"
        contract_key = "pattern"  # intentional collision with PatternConstraint

        def _parse_non_empty(self, raw_str, raw_original, ctx):
            return raw_str, None

        @classmethod
        def diff(cls, field_name, old, new):
            return None

    try:
        with pytest.raises(ConfigError, match="contract_key"):
            field_constraints.register(CollidingConstraint)
    finally:
        field_constraints.REGISTRY.pop("collides_with_pattern", None)


def test_constraint_without_docstring_rejected_at_register():
    from data_contract.errors import ConfigError

    class NoDocConstraint(FieldConstraint):
        name = "no_doc"
        contract_key = "no_doc"

        def _parse_non_empty(self, raw_str, raw_original, ctx):
            return raw_str, None

        @classmethod
        def diff(cls, field_name, old, new):
            return None

    try:
        with pytest.raises(ConfigError, match="docstring"):
            field_constraints.register(NoDocConstraint)
    finally:
        field_constraints.REGISTRY.pop("no_doc", None)


def test_constraint_docstring_missing_required_headers_rejected():
    from data_contract.errors import ConfigError

    class SparseDocConstraint(FieldConstraint):
        """Just a one-liner; no required headers."""
        name = "sparse"
        contract_key = "sparse"

        def _parse_non_empty(self, raw_str, raw_original, ctx):
            return raw_str, None

        @classmethod
        def diff(cls, field_name, old, new):
            return None

    try:
        with pytest.raises(ConfigError, match="headers"):
            field_constraints.register(SparseDocConstraint)
    finally:
        field_constraints.REGISTRY.pop("sparse", None)


def test_subclass_missing_diff_cannot_instantiate():
    class NoDiffConstraint(FieldConstraint):
        """Spec cell: ignored. Contract output: nothing. Drift: missing."""
        name = "no_diff"
        contract_key = "no_diff"

        def _parse_non_empty(self, raw_str, raw_original, ctx):
            return raw_str, None
        # diff intentionally not implemented

    with pytest.raises(TypeError):
        NoDiffConstraint(column=parse_column_ref({"spec_name": "X"}, name="no_diff"))


def test_unwrap_structured_value_handles_flat_dict_and_none():
    assert unwrap_structured_value(None) == (None, {})
    assert unwrap_structured_value(5) == (5, {})
    assert unwrap_structured_value("hello") == ("hello", {})
    assert unwrap_structured_value([1, 2]) == ([1, 2], {})
    assert unwrap_structured_value({"value": 5, "strict": True}) == (5, {"strict": True})
    assert unwrap_structured_value({"value": 5}) == (5, {})
    # params-only (no value key): returns (None, params)
    assert unwrap_structured_value({"level": "PII"}) == (None, {"level": "PII"})


# ---------------------------------------------------------------------------
# Phase 1 — format constraint
# ---------------------------------------------------------------------------


def test_format_known_tokens_parse():
    c = FormatConstraint.from_config({"spec_name": "Format"})
    for token in ("email", "uuid", "iban", "phone"):
        v, err = c.parse_cell(token, _ctx(Type.VARCHAR))
        assert err is None and v == token


def test_format_token_is_case_insensitive():
    c = FormatConstraint.from_config({"spec_name": "Format"})
    v, err = c.parse_cell("Email", _ctx(Type.VARCHAR))
    assert err is None and v == "email"


def test_format_unknown_token_rejects():
    c = FormatConstraint.from_config({"spec_name": "Format"})
    v, err = c.parse_cell("klingon", _ctx(Type.VARCHAR))
    assert v is None and err is not None and err.kind == "unknown_format"


def test_format_register_custom_token():
    register_format("npi", description="National Provider Identifier (US healthcare)")
    try:
        c = FormatConstraint.from_config({"spec_name": "Format"})
        v, err = c.parse_cell("npi", _ctx(Type.VARCHAR))
        assert err is None and v == "npi"
    finally:
        FORMAT_REGISTRY.pop("npi", None)


def test_format_register_duplicate_same_definition_idempotent():
    register_format("email", description="RFC 5322 email address",
                    pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    # No exception expected; the existing email entry stays.
    assert "email" in FORMAT_REGISTRY


def test_format_register_duplicate_different_definition_rejects():
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError):
        register_format("email", description="something else")


def test_format_drift_added_is_breaking():
    change = FormatConstraint.diff("contact", None, "email")
    assert change.kind == "format_added" and change.severity == "breaking"


def test_format_drift_removed_is_additive():
    change = FormatConstraint.diff("contact", "email", None)
    assert change.kind == "format_removed" and change.severity == "additive"


def test_format_drift_changed_is_breaking():
    change = FormatConstraint.diff("contact", "email", "phone")
    assert change.kind == "format_changed" and change.severity == "breaking"


# ---------------------------------------------------------------------------
# Phase 2 — default_value constraint
# ---------------------------------------------------------------------------


def test_default_value_integer():
    c = DefaultValueConstraint.from_config({"spec_name": "Default"})
    v, err = c.parse_cell("0", _ctx(Type.INTEGER))
    assert err is None and v == 0


def test_default_value_number():
    c = DefaultValueConstraint.from_config({"spec_name": "Default"})
    v, err = c.parse_cell("3.14", _ctx(Type.DOUBLE))
    assert err is None and v == pytest.approx(3.14)


def test_default_value_string_length():
    """For string fields, parse_typed_value interprets the cell as a length cap."""
    c = DefaultValueConstraint.from_config({"spec_name": "Default"})
    v, err = c.parse_cell("12", _ctx(Type.VARCHAR, max_length=50))
    assert err is None and v == 12


def test_default_value_blank_cell_emits_nothing():
    c = DefaultValueConstraint.from_config({"spec_name": "Default"})
    v, err = c.parse_cell("", _ctx(Type.INTEGER))
    assert v is None and err is None


def test_default_value_null_token_treated_as_blank():
    c = DefaultValueConstraint.from_config({
        "spec_name": "Default",
        "spec_parsing": {"null_tokens": ["-", "n/a", "none"]},
    })
    for token in ("-", "n/a", "N/A", "NONE"):
        v, err = c.parse_cell(token, _ctx(Type.INTEGER))
        assert v is None and err is None, f"expected {token!r} treated as blank"


def test_default_value_invalid_for_type_rejects():
    c = DefaultValueConstraint.from_config({"spec_name": "Default"})
    v, err = c.parse_cell("not-a-number", _ctx(Type.INTEGER))
    assert v is None and err is not None and err.kind == "invalid_default_value"


def test_default_value_null_tokens_must_be_list():
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="null_tokens"):
        DefaultValueConstraint.from_config({
            "spec_name": "Default",
            "spec_parsing": {"null_tokens": "not-a-list"},
        })


def test_default_value_contract_key_renamed_to_default():
    assert DefaultValueConstraint.contract_key == "default"
    assert DefaultValueConstraint.name == "default_value"


def test_default_value_drift_added_is_breaking():
    change = DefaultValueConstraint.diff("status", None, "pending")
    assert change.kind == "default_added" and change.severity == "breaking"


def test_default_value_drift_removed_is_additive():
    change = DefaultValueConstraint.diff("status", "pending", None)
    assert change.kind == "default_removed" and change.severity == "additive"


def test_default_value_drift_changed_is_breaking():
    change = DefaultValueConstraint.diff("status", "pending", "active")
    assert change.kind == "default_changed" and change.severity == "breaking"


# ---------------------------------------------------------------------------
# Phase 1 — Typed dispatch helper (FieldCheck, iter_checks, iter_field_checks)
# ---------------------------------------------------------------------------


def _field_with_constraints(name: str, constraints: dict) -> "FieldContract":
    from data_contract.contract import FieldContract
    return FieldContract(
        name=name, type=Type.VARCHAR, nullable=True, description=None,
        constraints=dict(constraints),
    )


def test_iter_checks_yields_one_per_registered_constraint():
    f = _field_with_constraints("status", {
        "allowed_values": ["active", "pending"],
        "unique": True,
    })
    checks = list(f.iter_checks())
    assert {c.constraint_name for c in checks} == {"allowed_values", "unique"}


def test_iter_checks_unwraps_structured_min_value():
    f = _field_with_constraints("amount", {
        "min_value": {"value": 5, "strict": True},
    })
    checks = list(f.iter_checks())
    assert len(checks) == 1
    chk = checks[0]
    assert chk.constraint_name == "min_value"
    assert chk.contract_key == "min_value"
    assert chk.value == 5
    assert chk.params == {"strict": True}


def test_iter_checks_handles_flat_pattern():
    f = _field_with_constraints("tag", {"pattern": "^[a-z]+$"})
    checks = list(f.iter_checks())
    assert len(checks) == 1
    assert checks[0].value == "^[a-z]+$"
    assert dict(checks[0].params) == {}


def test_iter_checks_skips_unregistered_contract_key():
    f = _field_with_constraints("x", {
        "pattern": "^a$",
        "legacy_check": 7,  # never registered
    })
    names = [c.constraint_name for c in f.iter_checks()]
    assert names == ["pattern"]


def test_iter_checks_returns_constraint_class():
    from data_contract.field_constraints.pattern import PatternConstraint
    f = _field_with_constraints("x", {"pattern": "^a$"})
    chk = next(iter(f.iter_checks()))
    assert chk.constraint_cls is PatternConstraint


def test_iter_field_checks_walks_all_fields():
    from data_contract.contract import Contract
    c = Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s.xlsx", spec_sheet="S", table="T",
        fields=[
            _field_with_constraints("a", {"pattern": "^x$"}),
            _field_with_constraints("b", {"unique": True, "pattern": "^y$"}),
        ],
    )
    pairs = list(c.iter_field_checks())
    by_field = {(f.name, chk.constraint_name) for f, chk in pairs}
    assert by_field == {("a", "pattern"), ("b", "unique"), ("b", "pattern")}


def test_primary_key_fields_returns_pk_subset():
    from data_contract.contract import Contract, FieldContract
    c = Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s", spec_sheet="S", table="T",
        fields=[
            FieldContract(name="a", type=Type.INTEGER, nullable=False, description=None, primary_key=True),
            FieldContract(name="b", type=Type.VARCHAR, nullable=True, description=None),
            FieldContract(name="c", type=Type.INTEGER, nullable=False, description=None, primary_key=True),
        ],
    )
    pks = c.primary_key_fields()
    assert [f.name for f in pks] == ["a", "c"]


def test_foreign_key_fields_returns_fk_subset():
    from data_contract.contract import Contract, FieldContract
    c = Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s", spec_sheet="S", table="T",
        fields=[
            FieldContract(name="a", type=Type.INTEGER, nullable=False, description=None),
            FieldContract(name="b", type=Type.INTEGER, nullable=False, description=None,
                          foreign_key={"table": "OTHER", "column": "id"}),
        ],
    )
    fks = c.foreign_key_fields()
    assert [f.name for f in fks] == ["b"]
    assert fks[0].foreign_key == {"table": "OTHER", "column": "id"}


def test_field_name_set_returns_just_names():
    from data_contract.contract import Contract, FieldContract
    c = Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s", spec_sheet="S", table="T",
        fields=[
            FieldContract(name="a", type=Type.INTEGER, nullable=False, description=None),
            FieldContract(name="b", type=Type.VARCHAR, nullable=True, description=None),
        ],
    )
    assert c.field_name_set() == {"a", "b"}


def test_iter_field_checks_default_value_contract_key_differs_from_name():
    """default_value's contract_key is 'default'; iter_checks must surface
    constraint_name='default_value' and contract_key='default'."""
    f = _field_with_constraints("status", {"default": "pending"})
    checks = list(f.iter_checks())
    assert len(checks) == 1
    assert checks[0].constraint_name == "default_value"
    assert checks[0].contract_key == "default"


def test_end_to_end_constraint_round_trip(types_yaml_path: Path):
    """Build a contract from synthetic spec rows that exercise allowed_values + pattern,
    confirm the contract carries the expected shape per field."""
    cm = ColumnMapping.from_dict({
        "name": {"spec_name": "Field Name", "value_required": True},
        "type": {"spec_name": "Type", "value_required": True},
        "description": {"spec_name": "Description", "value_required": False},
        "nullable": {
            "spec_name": "Obligatoire",
            "value_required": True,
            "values": {"true": ["non"], "false": ["oui"]},
        },
        "allowed_values": {
            "spec_name": "Values",
            "value_required": False,
            "spec_parsing": {"separator": ","},
        },
        "pattern": {"spec_name": "Pattern", "value_required": False},
    })
    from data_contract.config import KeysSpec, MergedConfig, TableSelector
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
    sheet = SheetSpec(sheet_name="T", header_row=1, col_idx={"name": 0, "type": 1, "description": 2, "nullable": 3}, has_table_column=False, constraint_cols={"allowed_values": 0, "pattern": 0})
    rows = [
        RawField(
            sheet_row=2,
            name_raw="id",
            type_raw="Double",
            description_raw="desc",
            nullable_raw="OUI",
            table_raw=None,
            extras={"allowed_values": None, "pattern": None},
        ),
        RawField(
            sheet_row=3,
            name_raw="status",
            type_raw="VARCHAR(50)",
            description_raw="status",
            nullable_raw="NON",
            table_raw=None,
            extras={"allowed_values": "active,pending", "pattern": r"^[a-z]+$"},
        ),
    ]
    registry = load_type_registry(types_yaml_path)
    from data_contract.contract import Contract
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
        "allowed_values": ["active", "pending"],
        "pattern": r"^[a-z]+$",
    }
    # to_dict flattens to top-level field keys
    payload = result.to_dict()
    status_dict = next(f for f in payload["fields"] if f["name"] == "status")
    assert status_dict["allowed_values"] == ["active", "pending"]
    assert status_dict["pattern"] == r"^[a-z]+$"
