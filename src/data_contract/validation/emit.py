"""Violation emission from a Polars LazyFrame of offending rows.

The per-phase checks (`validation/phases.py`) and the cross-table FK pass
(`validation/fk_pass.py`) both produce a LazyFrame whose rows describe
violations. This module collapses those frames into `Violation` objects on
the `TableReport`, carrying the per-row `__source_file__` / `__row_index__`
tracking columns and any primary-key context.
"""

from __future__ import annotations

from typing import Any

from data_contract.contract import FieldContract
from data_contract.validation.models import TableReport
from data_contract.violations import Violation


def emit_from_lazy(
    lazy_or_none,
    *,
    kind: str,
    severity: str,
    table: str,
    field: FieldContract | None,
    pk_cols: list[str],
    expected: str,
    report: TableReport,
    pk_violation: bool = False,
) -> None:
    """Materialise `lazy_or_none` (or no-op if None) and append a `Violation`
    per row to `report.violations`."""
    if lazy_or_none is None:
        return
    rows = lazy_or_none.collect().to_dicts()
    field_name = field.name if isinstance(field, FieldContract) else None
    for row in rows:
        pk_values = extract_pk_values(row, pk_cols)
        offending = row.get(field_name) if field_name else None
        report.violations.append(Violation(
            kind=kind, severity=severity, table=table,
            field=field_name if field_name else None,
            source_file=row.get("__source_file__"),
            source_row=int(row["__row_index__"]) if row.get("__row_index__") is not None else None,
            pk_values=pk_values, offending_value=offending, expected=expected,
        ))


def extract_pk_values(row: dict[str, Any], pk_cols: list[str]) -> dict[str, Any] | None:
    """Pluck the PK column values out of a row dict. Returns `None` when the
    table has no primary key (so reports don't carry an empty pk_values dict)."""
    if not pk_cols:
        return None
    return {c: row.get(c) for c in pk_cols}
