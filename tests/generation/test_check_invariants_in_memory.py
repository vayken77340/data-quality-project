"""Direct tests for `invariants.check_invariants_in_memory`."""

from __future__ import annotations

from pathlib import Path

import pytest

from dq_core.contract import Contract, FieldContract
from data_contract.generation.invariants import check_invariants_in_memory
from dq_core.type_mapping import Type, load_type_registry
from tests.conftest import TYPES_YAML, contract as _contract


def _registry():
    return load_type_registry(TYPES_YAML)


def test_in_memory_returns_per_table_dict_keyed_by_every_contract():
    """Even tables with zero errors get an entry (empty list)."""
    contracts = {
        "A": _contract("A", FieldContract(name="x", type=Type.STRING, nullable=True, description=None)),
        "B": _contract("B", FieldContract(name="y", type=Type.STRING, nullable=True, description=None)),
    }
    result = check_invariants_in_memory(contracts, None, _registry())
    assert set(result.per_table.keys()) == {"A", "B"}
    assert result.per_table["A"] == []
    assert result.per_table["B"] == []
    assert result.joins == []


def test_in_memory_joins_empty_list_when_none():
    """No joins contract -> empty joins list (never None)."""
    contracts = {"A": _contract("A", FieldContract(name="x", type=Type.STRING, nullable=True, description=None))}
    result = check_invariants_in_memory(contracts, None, _registry())
    assert result.joins == []


def test_in_memory_pk_must_not_be_nullable_flagged():
    """Per-table invariants surface in `per_table[table]`."""
    f = FieldContract(name="x", type=Type.STRING, nullable=True, description=None, primary_key=True)
    result = check_invariants_in_memory({"A": _contract("A", f)}, None, _registry())
    kinds = [e.kind for e in result.per_table["A"]]
    assert "pk_must_not_be_nullable" in kinds


def test_in_memory_fk_target_table_existence_checked_across_contracts():
    """FK whose target table isn't in `contracts_by_table` is flagged. The
    engine only runs FK existence when there ARE peer tables, so we add a
    sibling contract B that does NOT match the FK target."""
    f = FieldContract(
        name="ref", type=Type.STRING, nullable=False, description=None,
        foreign_key={"table": "MISSING", "column": "id"},
    )
    sibling = FieldContract(name="y", type=Type.STRING, nullable=True, description=None)
    contracts = {"A": _contract("A", f), "B": _contract("B", sibling)}
    result = check_invariants_in_memory(contracts, None, _registry())
    kinds = [e.kind for e in result.per_table["A"]]
    assert "fk_target_exists" in kinds
