from __future__ import annotations

import polars as pl

from dq_core.contract import FieldCheck, FieldContract
from dq_core.field_constraints.allowed_values import AllowedValuesConstraint
from dq_core.field_constraints.format import FormatConstraint
from dq_core.field_constraints.max_value import MaxValueConstraint
from dq_core.field_constraints.min_value import MinValueConstraint
from dq_core.field_constraints.pattern import PatternConstraint
from dq_core.field_constraints.unique import UniqueConstraint
from dq_core.type_mapping import Type
from tests.conftest import field_contract as _field


def _frame(rows: list[dict]) -> pl.LazyFrame:
    return pl.LazyFrame(rows)


def _check(cls, value, params=None) -> FieldCheck:
    return FieldCheck(
        constraint_name=cls.name,
        contract_key=cls.contract_key,
        constraint_cls=cls,
        value=value,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# allowed_values
# ---------------------------------------------------------------------------


def test_allowed_values_flags_non_member():
    frame = _frame([{"status": "active"}, {"status": "bogus"}, {"status": None}])
    violating = AllowedValuesConstraint.check_data(
        frame, _field("status"), _check(AllowedValuesConstraint, ["active", "pending"]),
    )
    rows = violating.collect().to_dicts()
    assert [r["status"] for r in rows] == ["bogus"]


def test_allowed_values_does_not_flag_nulls():
    frame = _frame([{"status": None}])
    violating = AllowedValuesConstraint.check_data(
        frame, _field("status"), _check(AllowedValuesConstraint, ["active"]),
    )
    assert violating.collect().height == 0


# ---------------------------------------------------------------------------
# pattern
# ---------------------------------------------------------------------------


def test_pattern_flags_non_matching():
    frame = _frame([{"code": "abc"}, {"code": "ABC"}, {"code": "ab1"}])
    violating = PatternConstraint.check_data(
        frame, _field("code"), _check(PatternConstraint, r"^[a-z]+$"),
    )
    codes = [r["code"] for r in violating.collect().to_dicts()]
    assert set(codes) == {"ABC", "ab1"}


# ---------------------------------------------------------------------------
# min_value / max_value
# ---------------------------------------------------------------------------


def test_min_value_non_strict():
    frame = _frame([{"x": 0}, {"x": 5}, {"x": 10}])
    violating = MinValueConstraint.check_data(
        frame, _field("x", Type.INT64), _check(MinValueConstraint, 5),
    )
    vals = [r["x"] for r in violating.collect().to_dicts()]
    assert vals == [0]


def test_min_value_strict_excludes_boundary():
    frame = _frame([{"x": 5}, {"x": 6}])
    violating = MinValueConstraint.check_data(
        frame, _field("x", Type.INT64), _check(MinValueConstraint, 5, {"strict": True}),
    )
    vals = [r["x"] for r in violating.collect().to_dicts()]
    assert vals == [5]


def test_max_value_non_strict():
    frame = _frame([{"x": 99}, {"x": 100}, {"x": 101}])
    violating = MaxValueConstraint.check_data(
        frame, _field("x", Type.INT64), _check(MaxValueConstraint, 100),
    )
    vals = [r["x"] for r in violating.collect().to_dicts()]
    assert vals == [101]


def test_max_value_strict_excludes_boundary():
    frame = _frame([{"x": 99}, {"x": 100}])
    violating = MaxValueConstraint.check_data(
        frame, _field("x", Type.INT64), _check(MaxValueConstraint, 100, {"strict": True}),
    )
    vals = [r["x"] for r in violating.collect().to_dicts()]
    assert vals == [100]


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------


def test_format_email_flags_bad_addresses():
    frame = _frame([
        {"contact": "alice@example.com"},
        {"contact": "not-an-email"},
        {"contact": None},
    ])
    violating = FormatConstraint.check_data(
        frame, _field("contact"), _check(FormatConstraint, "email"),
    )
    rows = violating.collect().to_dicts()
    assert [r["contact"] for r in rows] == ["not-an-email"]


def test_format_uuid_flags_bad_uuid():
    frame = _frame([
        {"id": "550e8400-e29b-41d4-a716-446655440000"},
        {"id": "not-a-uuid"},
    ])
    violating = FormatConstraint.check_data(
        frame, _field("id"), _check(FormatConstraint, "uuid"),
    )
    rows = violating.collect().to_dicts()
    assert [r["id"] for r in rows] == ["not-a-uuid"]


# ---------------------------------------------------------------------------
# unique
# ---------------------------------------------------------------------------


def test_unique_flags_every_participant_in_duplicate_cluster():
    frame = _frame([
        {"sku": "A"}, {"sku": "B"}, {"sku": "A"}, {"sku": "C"}, {"sku": "B"},
    ])
    violating = UniqueConstraint.check_data(
        frame, _field("sku"), _check(UniqueConstraint, True),
    )
    skus = sorted(r["sku"] for r in violating.collect().to_dicts())
    assert skus == ["A", "A", "B", "B"]


def test_unique_does_not_flag_null():
    frame = _frame([{"sku": None}, {"sku": None}])
    violating = UniqueConstraint.check_data(
        frame, _field("sku"), _check(UniqueConstraint, True),
    )
    assert violating.collect().height == 0
