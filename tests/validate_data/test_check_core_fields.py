from __future__ import annotations

import polars as pl

from data_contract.contract import FieldContract
from data_contract.type_mapping import Type
from data_contract.validate_data.checks.core_fields import (
    check_max_length,
    check_nullable,
    check_type_coercion,
)


def _frame(rows):
    return pl.LazyFrame(rows)


def _field(name, *, nullable=True, t=Type.VARCHAR, max_length=None):
    return FieldContract(name=name, type=t, nullable=nullable, description=None, max_length=max_length)


def test_check_nullable_flags_nulls_when_not_nullable():
    frame = _frame([{"x": "a"}, {"x": None}, {"x": "b"}])
    violating = check_nullable(frame, _field("x", nullable=False))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == [None]


def test_check_nullable_returns_none_when_nullable_true():
    assert check_nullable(_frame([]), _field("x", nullable=True)) is None


def test_check_nullable_returns_none_when_nullable_unset():
    assert check_nullable(_frame([]), _field("x", nullable=None)) is None


def test_check_max_length_flags_too_long():
    frame = _frame([{"x": "abc"}, {"x": "abcdef"}, {"x": None}])
    violating = check_max_length(frame, _field("x", t=Type.VARCHAR, max_length=4))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["abcdef"]


def test_check_max_length_skipped_for_non_string():
    assert check_max_length(_frame([]), _field("x", t=Type.INTEGER, max_length=5)) is None


def test_check_max_length_skipped_when_no_cap():
    assert check_max_length(_frame([]), _field("x", t=Type.VARCHAR)) is None


def test_check_type_coercion_flags_bad_integer():
    frame = pl.LazyFrame({"x": ["1", "2", "not-a-number", "3"]})
    violating = check_type_coercion(frame, _field("x", t=Type.INTEGER))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["not-a-number"]


def test_check_type_coercion_skipped_for_string():
    assert check_type_coercion(_frame([]), _field("x", t=Type.VARCHAR)) is None
