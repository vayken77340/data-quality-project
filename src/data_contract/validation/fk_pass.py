"""Cross-table FK existence pass.

After every per-table load + per-table check finishes, the runner schedules
the FK pass here. The pass walks each table's contract for `foreign_key` fields
and validates that the child's FK column values exist in the parent's PK column.

Gated per CHILD table: a table that disables `fk_existence` skips its own FK
columns regardless of what the parent table opts into.

Per-child gating keeps the policy intuitive — a contract owner controls what
checks run against their own table.
"""

from __future__ import annotations

from typing import Any

from dq_core.contract import Contract, FieldContract
from dq_core.table_checks.fk_existence import FkExistenceCheck as _FkCheckCls
from data_contract.validation.config import TableValidationConfig
from data_contract.validation.emit import emit_from_lazy
from dq_core.report_models import TableReport
from dq_core.violations import Violation


def run_cross_table_fk(
    *,
    table_reports: list[TableReport],
    table_frames: dict[str, Any],
    table_configs: dict[str, TableValidationConfig],
    contracts_by_table: dict[str, Contract],
) -> None:
    """For every child table with `fk_existence` enabled, validate each of its
    FK fields against the parent's PK column."""
    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        frame = table_frames.get(tr.table)
        if frame is None:
            continue
        child_cfg = table_configs.get(tr.table)
        if child_cfg is not None and not child_cfg.checks.is_enabled("fk_existence"):
            continue
        for fk_field in contract.foreign_key_fields():
            _run_fk_check(
                child_table=tr.table, child_frame=frame, fk_field=fk_field,
                contracts_by_table=contracts_by_table, table_frames=table_frames,
                report=tr,
            )


def _run_fk_check(
    *,
    child_table: str,
    child_frame: Any,
    fk_field: FieldContract,
    contracts_by_table: dict[str, Contract],
    table_frames: dict[str, Any],
    report: TableReport,
) -> None:
    if fk_field.foreign_key is None:
        return
    target_table = fk_field.foreign_key.get("table")
    target_column = fk_field.foreign_key.get("column")
    target_contract = contracts_by_table.get(target_table)
    target_frame = table_frames.get(target_table)
    if target_contract is None or target_frame is None:
        report.violations.append(Violation(
            kind="fk_target_table_not_loaded", severity="warning",
            table=child_table, field=fk_field.silver_name,
            expected=f"target table {target_table!r} included in this run",
        ))
        return
    violating = _FkCheckCls.run_for_field(
        child_frame, fk_field.silver_name, target_frame, target_column,
    )
    pk_cols = [f.silver_name for f in contracts_by_table[child_table].primary_key_fields()]
    emit_from_lazy(
        violating,
        kind=_FkCheckCls.VIOLATION_KIND, severity=_FkCheckCls.VIOLATION_SEVERITY,
        table=child_table, field=fk_field, pk_cols=pk_cols,
        expected=f"exists in {target_table}.{target_column}",
        report=report,
    )
