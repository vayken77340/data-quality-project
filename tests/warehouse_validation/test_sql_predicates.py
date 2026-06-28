"""Pure-function tests for the SQL predicate compilers + dispatch."""

from __future__ import annotations

from dq_core.contract import FieldCheck, FieldContract
from dq_core.field_constraints.allowed_values import AllowedValuesConstraint
from dq_core.field_constraints.format import FormatConstraint
from dq_core.field_constraints.max_value import MaxValueConstraint
from dq_core.field_constraints.min_value import MinValueConstraint
from dq_core.field_constraints.pattern import PatternConstraint
from dq_core.type_mapping import Type
from warehouse_validation.sql_predicates import (
    PushdownSQL,
    supported_constraint_names,
    to_sql_pushdown,
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
    p = to_sql_pushdown(
        _field("amount"), _check(MinValueConstraint, 0, strict=False),
        table_name="t",
    )
    assert p == PushdownSQL(kind="where", sql='"amount" < 0')


def test_min_value_strict():
    p = to_sql_pushdown(
        _field("amount"), _check(MinValueConstraint, 10, strict=True),
        table_name="t",
    )
    assert p == PushdownSQL(kind="where", sql='"amount" <= 10')


def test_min_value_default_strict_false():
    # strict omitted from params -> non-strict (matches MinValueConstraint default)
    p = to_sql_pushdown(
        _field("amount"), _check(MinValueConstraint, 5), table_name="t",
    )
    assert p == PushdownSQL(kind="where", sql='"amount" < 5')


# ---------------------------------------------------------------------------
# max_value
# ---------------------------------------------------------------------------


def test_max_value_non_strict():
    p = to_sql_pushdown(
        _field("amount"), _check(MaxValueConstraint, 100, strict=False),
        table_name="t",
    )
    assert p == PushdownSQL(kind="where", sql='"amount" > 100')


def test_max_value_strict():
    p = to_sql_pushdown(
        _field("amount"), _check(MaxValueConstraint, 100, strict=True),
        table_name="t",
    )
    assert p == PushdownSQL(kind="where", sql='"amount" >= 100')


# ---------------------------------------------------------------------------
# allowed_values
# ---------------------------------------------------------------------------


def test_allowed_values_simple_list():
    p = to_sql_pushdown(
        _field("status", Type.STRING),
        _check(AllowedValuesConstraint, ["A", "B", "C"]),
        table_name="t",
    )
    assert p == PushdownSQL(
        kind="where",
        sql='"status" IS NOT NULL AND "status" NOT IN (\'A\', \'B\', \'C\')',
    )


def test_allowed_values_escapes_single_quote():
    p = to_sql_pushdown(
        _field("owner", Type.STRING),
        _check(AllowedValuesConstraint, ["O'Hara"]),
        table_name="t",
    )
    assert p == PushdownSQL(
        kind="where",
        sql='"owner" IS NOT NULL AND "owner" NOT IN (\'O\'\'Hara\')',
    )


def test_allowed_values_empty_list_renders_empty_not_in():
    # Defensive: malformed contract with empty list. Polars side would have
    # rejected this at parse time; the predicate is still well-formed SQL
    # (the IS NOT NULL clause means every non-null row matches).
    p = to_sql_pushdown(
        _field("status", Type.STRING),
        _check(AllowedValuesConstraint, []),
        table_name="t",
    )
    assert p == PushdownSQL(
        kind="where",
        sql='"status" IS NOT NULL AND "status" NOT IN ()',
    )


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------


def test_format_known_token_emits_regexp_like():
    p = to_sql_pushdown(
        _field("contact_email", Type.STRING),
        _check(FormatConstraint, "email"),
        table_name="t",
    )
    assert p is not None
    assert p.kind == "where"
    assert '"contact_email" IS NOT NULL' in p.sql
    assert 'regexp_like("contact_email"' in p.sql
    # The email token's pattern is inlined as a quoted SQL string.
    assert "'^[A-Za-z0-9._%+-]+@" in p.sql


def test_format_unknown_token_returns_none():
    p = to_sql_pushdown(
        _field("x", Type.STRING),
        _check(FormatConstraint, "no_such_format"),
        table_name="t",
    )
    assert p is None


def test_format_token_without_pattern_returns_none(monkeypatch):
    # Register a pattern-less token and verify the predicate skips it.
    from dq_core.field_constraints.format import FORMAT_REGISTRY, FormatToken
    monkeypatch.setitem(
        FORMAT_REGISTRY, "info_only",
        FormatToken(name="info_only", description="d", pattern=None),
    )
    p = to_sql_pushdown(
        _field("x", Type.STRING),
        _check(FormatConstraint, "info_only"),
        table_name="t",
    )
    assert p is None


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_to_sql_pushdown_returns_none_for_unregistered_constraint():
    class _UnregisteredConstraint:
        name = "<<never-registered>>"
        contract_key = "<<never-registered>>"

    p = to_sql_pushdown(
        _field("code", Type.STRING),
        _check(_UnregisteredConstraint, "anything"),
        table_name="t",
    )
    assert p is None


# ---------------------------------------------------------------------------
# pattern
# ---------------------------------------------------------------------------


def test_pattern_simple_regex():
    p = to_sql_pushdown(
        _field("code", Type.STRING),
        _check(PatternConstraint, r"^[A-Z]{3}$"),
        table_name="t",
    )
    assert p is not None
    assert p.kind == "where"
    assert p.sql == (
        '"code" IS NOT NULL '
        'AND NOT regexp_like("code", \'^[A-Z]{3}$\')'
    )


def test_pattern_escapes_single_quote_in_regex():
    p = to_sql_pushdown(
        _field("name", Type.STRING),
        _check(PatternConstraint, "O'Brien"),
        table_name="t",
    )
    assert p is not None
    assert "'O''Brien'" in p.sql


def test_supported_constraint_names_lists_current_set():
    # As each new constraint lands, the assertion below grows.
    assert supported_constraint_names() == (
        "allowed_values", "format", "max_value", "min_value", "pattern",
    )
