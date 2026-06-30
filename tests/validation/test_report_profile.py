"""Tests for the per-field metadata profile builder.

Null / distinct counts are intentionally NOT tested here -- they live on
the metrics registry now. See tests/metrics/test_builtins.py for those.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dq_core.contract import Contract, FieldContract
from dq_core.targets import load_target_config
from dq_core.type_mapping import Type, load_type_registry
from dq_core.report.profile import (
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


# NOTE: kept local instead of using tests/conftest.py's `contract()` because
# every callsite expects `epic="T"` (the canonical's default is "E"). Passing
# `epic="T"` at every callsite was rejected in audit v7/v8 -- the epic
# string here is just placeholder noise that one-line shape-shifts the file.
def _contract(table: str, *fields: FieldContract) -> Contract:
    return Contract(
        version="1.0", epic="T", generated_at="",
        spec_file="", spec_sheet=table, table=table,
        fields=list(fields),
    )


def _f(name: str, t: Type, *, nullable=True, primary_key=None, foreign_key=None,
       max_length=None) -> FieldContract:
    return FieldContract(
        silver_name=name, type=t, nullable=nullable, description=None,
        max_length=max_length, primary_key=primary_key, foreign_key=foreign_key,
    )


# ---------------------------------------------------------------------------
# Metadata builder
# ---------------------------------------------------------------------------


def test_profile_carries_target_name_as_type_format(postgres_registry):
    contract = _contract("T", _f("x", Type.STRING, max_length=10))
    p = build_table_profile(
        contract, postgres_registry, total_rows=2, type_format="postgres",
    )
    assert p.fields[0].type_format == "postgres"
    # `type` is the target's physical type with max_length substituted.
    assert p.fields[0].type == "VARCHAR(10)"


def test_profile_pk_flag_carried(postgres_registry):
    contract = _contract("T", _f("id", Type.INT64, primary_key=True))
    p = build_table_profile(
        contract, postgres_registry, total_rows=3, type_format="postgres",
    )
    assert p.fields[0].is_pk is True
    assert p.fields[0].is_fk is False


def test_profile_fk_flag_carried(postgres_registry):
    contract = _contract(
        "T", _f("pid", Type.INT64, foreign_key={"table": "P", "column": "id"}),
    )
    p = build_table_profile(
        contract, postgres_registry, total_rows=2, type_format="postgres",
    )
    assert p.fields[0].is_fk is True


def test_profile_total_rows_passed_through(postgres_registry):
    contract = _contract("T", _f("x", Type.INT64))
    p = build_table_profile(
        contract, postgres_registry, total_rows=42, type_format="postgres",
    )
    assert p.total_rows == 42
    assert p.fields[0].total == 42


def test_profile_is_metadata_only_no_numeric_stats(postgres_registry):
    """FieldProfile carries metadata only -- numeric per-field stats live
    on `TableReport.metrics` (the metrics registry)."""
    contract = _contract("T", _f("x", Type.INT64))
    p = build_table_profile(
        contract, postgres_registry, total_rows=3, type_format="postgres",
    )
    f = p.fields[0]
    # Removed when profile became metadata-only.
    assert not hasattr(f, "null_count")
    assert not hasattr(f, "null_pct")
    assert not hasattr(f, "distinct_count")
    # Plus all the older fields that the leaner spec dropped.
    assert not hasattr(f, "min")
    assert not hasattr(f, "max")
    assert not hasattr(f, "top_values")
    assert not hasattr(f, "uniqueness_ratio")
    assert not hasattr(f, "canonical_type")


def test_profile_handles_field_not_in_contract_data_columns(postgres_registry):
    """Profile is contract-driven, so every contract field gets an entry --
    no need to know which columns are in the data frame."""
    contract = _contract(
        "T",
        _f("missing", Type.STRING, max_length=10),
        _f("other", Type.INT64),
    )
    p = build_table_profile(
        contract, postgres_registry, total_rows=3, type_format="postgres",
    )
    names = [f.name for f in p.fields]
    assert names == ["missing", "other"]
