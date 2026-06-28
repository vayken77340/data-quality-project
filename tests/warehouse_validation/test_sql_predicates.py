"""Pure-function tests for the three SQL predicate compilers + dispatch."""

from __future__ import annotations

from dq_core.contract import FieldCheck, FieldContract
from dq_core.field_constraints.allowed_values import AllowedValuesConstraint
from dq_core.field_constraints.max_value import MaxValueConstraint
from dq_core.field_constraints.min_value import MinValueConstraint
from dq_core.field_constraints.pattern import PatternConstraint
from dq_core.type_mapping import Type
from warehouse_validation.sql_predicates import (
    supported_constraint_names,
    to_sql_predicate,
)


def _field(name: str, type_: Type = Type.INT64) -> FieldContract:
    return FieldContract(name=name, type=type_, nullable=True, description="")


def _check(cls, value, **params) -> FieldCheck:
    return FieldCheck(
        constraint_name=cls.name,
        contract_key=cls.contract_key,
        constraint_cls=cls,
        value=value,
        params=params,
    )


# ---------------------------------------------------------------------------
# min_value
# ---------------------------------------------------------------------------


def test_min_value_non_strict():
    sql = to_sql_predicate(_field("amount"), _check(MinValueConstraint, 0, strict=False))
    assert sql == '"amount" < 0'


def test_min_value_strict():
    sql = to_sql_predicate(_field("amount"), _check(MinValueConstraint, 10, strict=True))
    assert sql == '"amount" <= 10'


def test_min_value_default_strict_false():
    # strict omitted from params -> non-strict (matches MinValueConstraint default)
    sql = to_sql_predicate(_field("amount"), _check(MinValueConstraint, 5))
    assert sql == '"amount" < 5'


# ---------------------------------------------------------------------------
# max_value
# ---------------------------------------------------------------------------


def test_max_value_non_strict():
    sql = to_sql_predicate(_field("amount"), _check(MaxValueConstraint, 100, strict=False))
    assert sql == '"amount" > 100'


def test_max_value_strict():
    sql = to_sql_predicate(_field("amount"), _check(MaxValueConstraint, 100, strict=True))
    assert sql == '"amount" >= 100'


# ---------------------------------------------------------------------------
# allowed_values
# ---------------------------------------------------------------------------


def test_allowed_values_simple_list():
    sql = to_sql_predicate(
        _field("status", Type.STRING),
        _check(AllowedValuesConstraint, ["A", "B", "C"]),
    )
    assert sql == '"status" IS NOT NULL AND "status" NOT IN (\'A\', \'B\', \'C\')'


def test_allowed_values_escapes_single_quote():
    sql = to_sql_predicate(
        _field("owner", Type.STRING),
        _check(AllowedValuesConstraint, ["O'Hara"]),
    )
    assert sql == '"owner" IS NOT NULL AND "owner" NOT IN (\'O\'\'Hara\')'


def test_allowed_values_empty_list_renders_empty_not_in():
    # Defensive: malformed contract with empty list. Polars side would have
    # rejected this at parse time; the predicate is still well-formed SQL
    # (the IS NOT NULL clause means every non-null row matches).
    sql = to_sql_predicate(
        _field("status", Type.STRING),
        _check(AllowedValuesConstraint, []),
    )
    assert sql == '"status" IS NOT NULL AND "status" NOT IN ()'


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_to_sql_predicate_returns_none_for_unregistered_constraint():
    # PatternConstraint exists in dq_core but has no SQL pushdown in Phase 2.
    sql = to_sql_predicate(
        _field("code", Type.STRING),
        _check(PatternConstraint, r"^\d{3}$"),
    )
    assert sql is None


def test_supported_constraint_names_lists_phase_2_set():
    assert supported_constraint_names() == ("allowed_values", "max_value", "min_value")
