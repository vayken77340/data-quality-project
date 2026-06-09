"""Tests for the per-field data profile builder."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from data_contract.contract import Contract, FieldContract
from data_contract.errors import ConfigError
from data_contract.targets import load_target_config
from data_contract.type_mapping import Type, load_type_registry
from data_contract.validate_data.report.profile import (
    FieldProfile,
    TableProfile,
    build_table_profile,
)


@pytest.fixture(scope="module")
def postgres_registry():
    """Most profile tests don't care which target -- they just need an active
    one because type_registry.physical_type_for() requires it."""
    repo = Path(__file__).resolve().parents[2]
    base = load_type_registry(repo / "configs" / "types.yaml")
    pg = load_target_config(repo / "configs" / "targets" / "postgres.yaml")
    return base.with_target(pg)


def _contract(table: str, *fields: FieldContract) -> Contract:
    return Contract(
        version="1.0", epic="T", generated_at="",
        spec_file="", spec_sheet=table, table=table,
        fields=list(fields),
    )


def _f(name: str, t: Type, *, nullable=True, primary_key=None, foreign_key=None,
       max_length=None) -> FieldContract:
    return FieldContract(
        name=name, type=t, nullable=nullable, description=None,
        max_length=max_length, primary_key=primary_key, foreign_key=foreign_key,
    )


# ---------------------------------------------------------------------------
# Basic field profiling
# ---------------------------------------------------------------------------


def test_profile_carries_target_name_as_type_format(postgres_registry):
    df = pl.DataFrame({"x": ["a", "b"]})
    contract = _contract("T", _f("x", Type.STRING, max_length=10))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    assert p.fields[0].type_format == "postgres"
    # `type` is the target's physical type with max_length substituted.
    assert p.fields[0].type == "VARCHAR(10)"


def test_profile_int_field_counts(postgres_registry):
    df = pl.DataFrame({"x": [1, 2, 3, 4, 5]}, schema={"x": pl.Int64})
    contract = _contract("T", _f("x", Type.INT64))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    f = p.fields[0]
    assert f.total == 5
    assert f.null_count == 0
    assert f.null_pct == 0.0
    assert f.distinct_count == 5


def test_profile_string_field_with_nulls(postgres_registry):
    df = pl.DataFrame({"x": ["a", "b", "a", "c", None]})
    contract = _contract("T", _f("x", Type.STRING, max_length=10))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    f = p.fields[0]
    assert f.null_count == 1
    assert f.null_pct == 20.0
    assert f.distinct_count == 3   # a, b, c (nulls excluded)


def test_profile_pk_flag_carried(postgres_registry):
    df = pl.DataFrame({"id": [1, 2, 3]}, schema={"id": pl.Int64})
    contract = _contract("T", _f("id", Type.INT64, primary_key=True))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    assert p.fields[0].is_pk is True
    assert p.fields[0].is_fk is False


def test_profile_fk_flag_carried(postgres_registry):
    df = pl.DataFrame({"pid": [1, 2]}, schema={"pid": pl.Int64})
    contract = _contract(
        "T", _f("pid", Type.INT64, foreign_key={"table": "P", "column": "id"}),
    )
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    assert p.fields[0].is_fk is True


def test_profile_missing_field_gets_placeholder(postgres_registry):
    df = pl.DataFrame({"other": [1, 2, 3]}, schema={"other": pl.Int64})
    contract = _contract(
        "T", _f("missing", Type.STRING, max_length=10), _f("other", Type.INT64),
    )
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    missing = next(f for f in p.fields if f.name == "missing")
    assert missing.null_count == 3
    assert missing.null_pct == 100.0
    assert missing.distinct_count == 0


def test_profile_empty_table(postgres_registry):
    df = pl.DataFrame({"x": []}, schema={"x": pl.String})
    contract = _contract("T", _f("x", Type.STRING, max_length=10))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    f = p.fields[0]
    assert f.total == 0
    assert f.null_count == 0
    assert f.distinct_count == 0


def test_profile_no_min_max_top_values_fields(postgres_registry):
    """Confirm the slimmed shape: FieldProfile carries only the columns
    the spec calls for (no min/max/top_values/uniqueness_ratio)."""
    df = pl.DataFrame({"x": [1, 2, 3]}, schema={"x": pl.Int64})
    contract = _contract("T", _f("x", Type.INT64))
    p = build_table_profile(df, contract, postgres_registry, type_format="postgres")
    f = p.fields[0]
    # These attributes were removed; access via getattr to detect their absence.
    assert not hasattr(f, "min")
    assert not hasattr(f, "max")
    assert not hasattr(f, "top_values")
    assert not hasattr(f, "uniqueness_ratio")
    assert not hasattr(f, "canonical_type")
