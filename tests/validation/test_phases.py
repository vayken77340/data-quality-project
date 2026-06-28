"""Direct tests for the `validation.phases` PHASES tuple.

Locks down ordering (boolean before typed-coercion before nullable/max_length
before field-constraints before table-checks) and verifies each phase honours
its gate while still normalising the frame.
"""

from __future__ import annotations

import polars as pl
import pytest

from dq_core.contract import Contract, FieldContract
from dq_core.gates import Gates, GateSpec
from dq_core.type_mapping import Type, load_type_registry
from data_contract.validation.emit import emit_from_lazy
from dq_core.report_models import TableReport
from data_contract.validation.phases import (
    PHASES,
    PhaseContext,
    run_boolean_phase,
    run_nullable_max_length_phase,
    run_table_checks_phase,
    run_typed_coercion_phase,
)
from tests.conftest import TYPES_YAML


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _type_registry():
    return load_type_registry(TYPES_YAML)


def _gates(enabled: set[str] | None = None) -> Gates:
    """Build a Gates instance with the named checks enabled (others disabled)."""
    enabled = enabled or set()
    spec_map = {
        name: GateSpec(enabled=name in enabled)
        for name in (
            "type_coercion", "boolean_coercion", "nullable", "max_length",
            "field_names_from_sample", "field_types_from_sample",
            "column_missing", "pk_uniqueness", "fk_existence",
            "allowed_values", "pattern", "min_value", "max_value", "format", "unique",
        )
    }
    tier_of = {name: ("structural" if name in {
        "type_coercion", "boolean_coercion", "nullable", "max_length",
        "field_names_from_sample", "field_types_from_sample",
    } else "table" if name in {
        "column_missing", "pk_uniqueness", "fk_existence",
    } else "field") for name in spec_map}
    return Gates(specs=spec_map, tier_of=tier_of)


def _ctx(df, contract: Contract, gates: Gates) -> PhaseContext:
    return PhaseContext(
        df=df, contract=contract, gates=gates,
        type_registry=_type_registry(),
        report=TableReport(
            table=contract.table, contract_version=contract.version,
            pk_fields=[], input_files=[],
        ),
        data_columns={c for c in df.columns if not c.startswith("__")},
        pk_cols=[],
        emit=emit_from_lazy,
    )


# ---------------------------------------------------------------------------
# Order / discoverability
# ---------------------------------------------------------------------------


def test_phases_order_is_load_bearing():
    """Boolean must precede typed coercion (booleans need their own coercion
    path); nullable / max_length must come after typed coercion (they read
    normalised columns); table checks run last (pk_uniqueness sees the post-
    coercion frame)."""
    from data_contract.validation.phases import (
        run_field_constraints_phase,
    )
    assert PHASES == (
        run_boolean_phase,
        run_typed_coercion_phase,
        run_nullable_max_length_phase,
        run_field_constraints_phase,
        run_table_checks_phase,
    )


# ---------------------------------------------------------------------------
# Boolean phase
# ---------------------------------------------------------------------------


def test_boolean_phase_emits_when_gate_on_invalid_token():
    """A non-recognised boolean token raises a `boolean_coercion_violation`."""
    contract = Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="T",
        fields=[FieldContract(
            name="flag", type=Type.BOOLEAN, nullable=False, description=None,
            data_values={"true": ["yes"], "false": ["no"]},
        )],
    )
    df = pl.DataFrame({
        "flag": ["yes", "MAYBE"],
        "__source_file__": ["f.csv", "f.csv"],
        "__row_index__": [1, 2],
    })
    ctx = _ctx(df, contract, _gates({"boolean_coercion"}))
    run_boolean_phase(ctx)
    kinds = [v.kind for v in ctx.report.violations]
    assert "boolean_coercion_violation" in kinds


def test_boolean_phase_normalises_even_when_gate_off():
    """Token normalisation must run regardless of gate; downstream phases
    require canonical pl.Boolean."""
    contract = Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="T",
        fields=[FieldContract(
            name="flag", type=Type.BOOLEAN, nullable=True, description=None,
            data_values={"true": ["yes"], "false": ["no"]},
        )],
    )
    df = pl.DataFrame({
        "flag": ["yes", "no"],
        "__source_file__": ["f.csv", "f.csv"],
        "__row_index__": [1, 2],
    })
    ctx = _ctx(df, contract, _gates())   # boolean_coercion off
    run_boolean_phase(ctx)
    assert ctx.df.schema["flag"] == pl.Boolean
    assert ctx.report.violations == []


# ---------------------------------------------------------------------------
# Typed coercion phase
# ---------------------------------------------------------------------------


def test_typed_coercion_phase_skips_string_text_unknown_boolean():
    """STRING / TEXT / UNKNOWN coerce trivially; BOOLEAN has its own phase."""
    contract = Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="T",
        fields=[
            FieldContract(name="s", type=Type.STRING, nullable=True, description=None),
        ],
    )
    df = pl.DataFrame({
        "s": ["x", "y"],
        "__source_file__": ["f.csv", "f.csv"],
        "__row_index__": [1, 2],
    })
    ctx = _ctx(df, contract, _gates({"type_coercion"}))
    run_typed_coercion_phase(ctx)
    assert ctx.report.violations == []
    # Frame unchanged.
    assert ctx.df.schema["s"] == pl.String


# ---------------------------------------------------------------------------
# Nullable / max_length phase
# ---------------------------------------------------------------------------


def test_nullable_phase_emits_when_gate_on():
    contract = Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="T",
        fields=[FieldContract(name="x", type=Type.STRING, nullable=False, description=None)],
    )
    df = pl.DataFrame({
        "x": ["v", None],
        "__source_file__": ["f.csv", "f.csv"],
        "__row_index__": [1, 2],
    })
    ctx = _ctx(df, contract, _gates({"nullable"}))
    run_nullable_max_length_phase(ctx)
    kinds = [v.kind for v in ctx.report.violations]
    assert "nullable_violation" in kinds


def test_nullable_phase_skips_when_gate_off():
    contract = Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="T",
        fields=[FieldContract(name="x", type=Type.STRING, nullable=False, description=None)],
    )
    df = pl.DataFrame({
        "x": [None],
        "__source_file__": ["f.csv"],
        "__row_index__": [1],
    })
    ctx = _ctx(df, contract, _gates())
    run_nullable_max_length_phase(ctx)
    assert ctx.report.violations == []
