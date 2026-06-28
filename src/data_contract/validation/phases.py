"""Per-table check pipeline.

The runner used to inline a 270-line block of phases inside
`_validate_one_table`. Each phase has the same shape: walk the contract
fields, decide if the gate is enabled, run a Polars-side check, emit any
violating rows. This module hosts the phases as small functions sharing
a `PhaseContext`; the runner just iterates the `PHASES` list.

Why phases instead of free-flowing code:
  * Order is load-bearing -- boolean coercion must precede type coercion,
    type normalisation must precede nullable/max_length/constraint checks.
    Explicit ordering > implicit reading-order.
  * Adding a new structural check is one entry on the PHASES list, not
    a hunt-the-right-spot insertion.

Cross-table FK doesn't appear here -- it runs in a second pass after every
table has been loaded; see `run_cross_table_fk` in validation/fk_pass.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from dq_core.contract import Contract
from dq_core.type_mapping import Type, TypeRegistry
from data_contract.validation.config import Gates
from dq_core.report_models import TableReport
from dq_core.report_build import (
    describe_constraint,
    type_coercion_expected,
)


@dataclass
class PhaseContext:
    """Mutable per-table state threaded through every phase.

    `df` is replaced after each typed-normalisation phase so downstream
    phases see correctly-typed columns. Phases mutate `df` in place via
    rebinding; the runner picks it up after every phase returns.
    """
    df: Any                        # eager pl.DataFrame
    contract: Contract
    gates: Gates
    type_registry: TypeRegistry
    report: TableReport
    data_columns: set[str]
    pk_cols: list[str]
    emit: Callable[..., None]      # bound `_emit_from_lazy` from runner.py


# ---------------------------------------------------------------------------
# Structural phases
# ---------------------------------------------------------------------------


def run_boolean_phase(ctx: PhaseContext) -> None:
    """Per-field boolean coercion + canonical normalisation.

    Token source is per-field: `fc.data_values` (stamped onto the contract
    at generation time). Targets do not control accepted tokens -- the
    contract does. Normalisation runs even when `boolean_coercion` is
    disabled because downstream phases require canonical `pl.Boolean`.
    """
    from data_contract.validation.checks.core_fields import (
        _field_boolean_tokens,
        check_boolean_coercion,
        normalize_boolean_column,
    )

    for fc in ctx.contract.fields:
        if fc.type is not Type.BOOLEAN or fc.name not in ctx.data_columns:
            continue
        field_tokens = _field_boolean_tokens(fc, ctx.type_registry)
        if field_tokens is None:
            continue
        if ctx.gates.is_enabled("boolean_coercion"):
            accepted = sorted({
                *field_tokens.get("true", set()),
                *field_tokens.get("false", set()),
            })
            ctx.emit(
                check_boolean_coercion(ctx.df.lazy(), fc, ctx.type_registry),
                kind="boolean_coercion_violation", severity="error",
                table=ctx.contract.table, field=fc, pk_cols=ctx.pk_cols,
                expected=f"one of: {', '.join(accepted)}",
                report=ctx.report,
            )
        ctx.df = normalize_boolean_column(ctx.df, fc, ctx.type_registry)


def run_typed_coercion_phase(ctx: PhaseContext) -> None:
    """Per-field typed coercion + canonical normalisation.

    Skips VARCHAR / TEXT / UNKNOWN (everything coerces to text) and BOOLEAN
    (handled by `run_boolean_phase`). Normalisation runs even when the
    gate is off so downstream Polars dtype invariants hold.
    """
    from data_contract.validation.checks.core_fields import (
        check_type_coercion,
        normalize_typed_column,
    )

    for fc in ctx.contract.fields:
        if fc.name not in ctx.data_columns:
            continue
        if fc.type in (Type.STRING, Type.TEXT, Type.UNKNOWN, Type.BOOLEAN):
            continue
        if ctx.gates.is_enabled("type_coercion"):
            ctx.emit(
                check_type_coercion(ctx.df.lazy(), fc, ctx.type_registry, eager_df=ctx.df),
                kind="type_coercion_violation", severity="error",
                table=ctx.contract.table, field=fc, pk_cols=ctx.pk_cols,
                expected=type_coercion_expected(fc, ctx.type_registry),
                report=ctx.report,
            )
        ctx.df = normalize_typed_column(ctx.df, fc, ctx.type_registry)


def run_nullable_max_length_phase(ctx: PhaseContext) -> None:
    """Per-field nullable + VARCHAR max_length.

    Runs after typed normalisation so `nullable` sees nulls produced by
    failed coercion; `max_length` only ever runs on VARCHAR (still String,
    never normalised).
    """
    from data_contract.validation.checks.core_fields import (
        check_max_length,
        check_nullable,
    )

    for fc in ctx.contract.fields:
        if fc.name not in ctx.data_columns:
            continue
        if ctx.gates.is_enabled("nullable"):
            ctx.emit(
                check_nullable(ctx.df.lazy(), fc),
                kind="nullable_violation", severity="error",
                table=ctx.contract.table, field=fc, pk_cols=ctx.pk_cols,
                expected="required", report=ctx.report,
            )
        if ctx.gates.is_enabled("max_length"):
            ctx.emit(
                check_max_length(ctx.df.lazy(), fc, ctx.type_registry),
                kind="max_length_violation", severity="error",
                table=ctx.contract.table, field=fc, pk_cols=ctx.pk_cols,
                expected=f"max {fc.max_length} chars",
                report=ctx.report,
            )


def run_field_constraints_phase(ctx: PhaseContext) -> None:
    """Per-constraint dispatch via each constraint class's `check_data`.

    Each constraint gates by its own `name` so authors can disable individual
    constraint families (e.g. `min_value: false`) without losing the others.
    """
    for fc, check in ctx.contract.iter_field_checks():
        if fc.name not in ctx.data_columns:
            continue
        if not ctx.gates.is_enabled(check.constraint_cls.name):
            continue
        violating = check.constraint_cls.check_data(ctx.df.lazy(), fc, check)
        if violating is None:
            continue
        ctx.emit(
            violating,
            kind=check.constraint_cls.VIOLATION_KIND,
            severity=check.constraint_cls.VIOLATION_SEVERITY,
            table=ctx.contract.table, field=fc, pk_cols=ctx.pk_cols,
            expected=describe_constraint(check),
            report=ctx.report,
        )


def run_table_checks_phase(ctx: PhaseContext) -> None:
    """Non-cross-table TableCheck registry entries.

    `column_missing` (table-scope, returns list[Violation]) and
    `pk_uniqueness` (row-scope) are both handled here. Cross-table checks
    (FK existence) are scheduled in the post-all-tables phase by the runner.

    `pk_uniqueness` is intentionally last so it runs after the per-field
    phases -- a row whose PK columns failed type coercion shouldn't also
    show up as "not unique".
    """
    from dq_core import table_checks as _table_checks_pkg

    pk_uniqueness_pending: type | None = None

    for check_name, check_cls in _table_checks_pkg.REGISTRY.items():
        if check_cls.requires_cross_table:
            continue
        if not ctx.gates.is_enabled(check_name):
            continue
        if check_name == "pk_uniqueness":
            pk_uniqueness_pending = check_cls
            continue
        _dispatch_table_check(ctx, check_cls)

    if pk_uniqueness_pending is not None:
        _dispatch_table_check(ctx, pk_uniqueness_pending, pk_violation=True)


def _dispatch_table_check(
    ctx: PhaseContext, check_cls: type, *, pk_violation: bool = False,
) -> None:
    result = check_cls().check_data(
        ctx.df.lazy(), ctx.contract, data_columns=ctx.data_columns,
    )
    if result is None:
        return
    if isinstance(result, list):
        ctx.report.violations.extend(result)
        return
    ctx.emit(
        result,
        kind=check_cls.VIOLATION_KIND,
        severity=check_cls.VIOLATION_SEVERITY,
        table=ctx.contract.table,
        field=None,
        pk_cols=ctx.pk_cols,
        expected=(
            "must be unique" if pk_violation
            else check_cls.description or check_cls.name
        ),
        report=ctx.report,
        pk_violation=pk_violation,
    )


PHASES: tuple[Callable[[PhaseContext], None], ...] = (
    run_boolean_phase,
    run_typed_coercion_phase,
    run_nullable_max_length_phase,
    run_field_constraints_phase,
    run_table_checks_phase,
)
