from __future__ import annotations

import polars as pl

from data_contract.contract import Contract, FieldContract
from data_contract.type_mapping import Type
from data_contract.validate_data.checks.keys import (
    check_fk_existence,
    check_pk_uniqueness,
)


def _contract(table, fields):
    return Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s", spec_sheet="S", table=table, fields=fields,
    )


def _f(name, *, t=Type.INTEGER, nullable=False, primary_key=None):
    return FieldContract(name=name, type=t, nullable=nullable, description=None, primary_key=primary_key)


def test_pk_uniqueness_returns_none_when_no_pk():
    contract = _contract("T", [_f("x")])
    frame = pl.LazyFrame([{"x": 1}, {"x": 2}])
    assert check_pk_uniqueness(frame, contract) is None


def test_pk_uniqueness_flags_every_participant():
    contract = _contract("T", [_f("proj_id", primary_key=True)])
    frame = pl.LazyFrame([
        {"proj_id": 1},
        {"proj_id": 2},
        {"proj_id": 1},
        {"proj_id": 3},
        {"proj_id": 2},
    ])
    violating = check_pk_uniqueness(frame, contract)
    ids = sorted(r["proj_id"] for r in violating.collect().to_dicts())
    # Both 1s and both 2s are flagged; 3 is fine.
    assert ids == [1, 1, 2, 2]


def test_pk_uniqueness_skips_null_pk():
    contract = _contract("T", [_f("proj_id", primary_key=True, nullable=True)])
    frame = pl.LazyFrame([{"proj_id": None}, {"proj_id": None}, {"proj_id": 1}])
    violating = check_pk_uniqueness(frame, contract)
    assert violating.collect().height == 0


def test_pk_uniqueness_composite():
    contract = _contract("T", [
        _f("order_id", primary_key=True),
        _f("line_id", primary_key=True),
        _f("note", t=Type.VARCHAR, nullable=True),
    ])
    frame = pl.LazyFrame([
        {"order_id": 1, "line_id": 1, "note": "a"},
        {"order_id": 1, "line_id": 2, "note": "b"},  # different composite, not a violation
        {"order_id": 1, "line_id": 1, "note": "c"},  # duplicate of row 1
    ])
    violating = check_pk_uniqueness(frame, contract)
    rows = violating.collect().to_dicts()
    notes = sorted(r["note"] for r in rows)
    assert notes == ["a", "c"]


def test_fk_existence_flags_dangling_reference():
    child = pl.LazyFrame([
        {"id": 1, "parent_id": 100},
        {"id": 2, "parent_id": 999},  # dangling
        {"id": 3, "parent_id": 200},
    ])
    parent = pl.LazyFrame([{"pk": 100}, {"pk": 200}, {"pk": 300}])
    violating = check_fk_existence(child, "parent_id", parent, "pk")
    rows = violating.collect().to_dicts()
    assert [r["parent_id"] for r in rows] == [999]


def test_fk_existence_does_not_flag_null():
    child = pl.LazyFrame([{"parent_id": None}, {"parent_id": 100}])
    parent = pl.LazyFrame([{"pk": 100}])
    violating = check_fk_existence(child, "parent_id", parent, "pk")
    assert violating.collect().height == 0
