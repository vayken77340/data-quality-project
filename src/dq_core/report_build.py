"""Post-check builders.

The runner streams violations into each TableReport as checks execute.
Once every per-table phase has run (and the cross-table FK phase too),
the helpers here transform those violations into the higher-level
artifacts every report format expects:

  * `populate_by_check`     -- per-(table, check) status grid.
  * `populate_metrics`      -- run the metrics registry over the eager df.
  * `build_rejected_rows`   -- row-centric aggregation with full source-row
                               context for the rejected-rows sheet.
  * `build_run_metadata`    -- run-level metadata (target, checks, contracts).
  * `describe_constraint` / `type_coercion_expected` -- compact "Expected"
                               labels that flow into Violation.expected at
                               emit time. They live here because they're
                               consumed BEFORE report rendering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dq_core.contract import Contract, FieldCheck, FieldContract
from dq_core.gates import Gates
from dq_core.report_models import (
    RejectedRow,
    RunMetadata,
    TableReport,
)
from dq_core.type_mapping import Type, TypeRegistry
from dq_core.violations import Violation

if TYPE_CHECKING:
    from data_contract.validation.config import ValidationConfig


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


# ---------------------------------------------------------------------------
# Per-check status + metrics
# ---------------------------------------------------------------------------


def populate_by_check(report: TableReport, checks: Gates) -> None:
    """Compute report.by_check from the current violation list."""
    from dq_core.report.check_status import compute_table_check_status
    from dq_core.report.dimensions import _CHECK_TO_VIOLATION_KIND

    enabled_map = {name: checks.is_enabled(name) for name in _CHECK_TO_VIOLATION_KIND}
    report.by_check = compute_table_check_status(
        total_rows=report.total_rows,
        violations=report.violations,
        enabled_checks=enabled_map,
        check_to_kind=dict(_CHECK_TO_VIOLATION_KIND),
    )


def populate_metrics(
    report: TableReport, df, contract: Contract, gates: Gates, type_registry: TypeRegistry,
) -> None:
    """Compute report.metrics by iterating the metrics registry."""
    from dq_core import metrics as _metrics_pkg

    out: dict[str, Any] = {}
    for name, cls in _metrics_pkg.REGISTRY.items():
        if not gates.is_enabled(name):
            continue
        out[name] = cls().compute(df, contract, type_registry)
    report.metrics = out


# ---------------------------------------------------------------------------
# Expected-string builders (consumed at violation-emit time)
# ---------------------------------------------------------------------------


_TYPE_EXPECTED_LABEL: dict[Type, str] = {
    Type.INT32: "integer",
    Type.INT64: "integer",
    Type.FLOAT32: "decimal",
    Type.FLOAT64: "decimal",
    Type.DATE: "date",
    Type.TIMESTAMP: "timestamp",
    Type.TIMESTAMP_TZ: "timestamp",
    Type.BOOLEAN: "boolean",
}


def type_coercion_expected(field: FieldContract, type_registry: TypeRegistry) -> str:
    """Terse type label for the Expected cell.

    Examples (concrete format hints, physical-type detail, etc.) belong in the
    hint and the top-values drill-down -- not in this cell.
    """
    return _TYPE_EXPECTED_LABEL.get(field.type, field.type.value)


_ALLOWED_VALUES_PREVIEW = 5
_PATTERN_INLINE_MAX = 40


def describe_constraint(check: FieldCheck) -> str:
    cls = check.constraint_cls
    if cls.name == "min_value":
        op = ">" if check.params.get("strict") else ">="
        return f"{op} {check.value}"
    if cls.name == "max_value":
        op = "<" if check.params.get("strict") else "<="
        return f"{op} {check.value}"
    if cls.name == "allowed_values":
        values = list(check.value or [])
        preview = ", ".join(str(x) for x in values[:_ALLOWED_VALUES_PREVIEW])
        extra = len(values) - _ALLOWED_VALUES_PREVIEW
        if extra > 0:
            return f"one of: {preview} (+{extra} more)"
        return f"one of: {preview}"
    if cls.name == "pattern":
        pattern = str(check.value)
        if len(pattern) > _PATTERN_INLINE_MAX:
            return "matches pattern"
        return f"matches /{pattern}/"
    if cls.name == "format":
        return f"format {check.value}"
    if cls.name == "unique":
        return "unique"
    return cls.VIOLATION_KIND or cls.name


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------


def build_run_metadata(
    *,
    epic: str,
    generated_at: str,
    duration_ms: int,
    config: ValidationConfig,
    target_config,
    contracts_by_table: dict[str, Contract],
    types_path: Path,
    table_reports: list[TableReport],
) -> RunMetadata:
    """Assemble the per-run context that every report format surfaces."""
    from data_contract import __version__ as _tool_version
    from dq_core.report.strings import load_strings

    enabled: list[str] = []
    disabled: list[str] = []
    descriptions: dict[str, str] = {}
    try:
        descriptions_map = load_strings().get("checks_sheet", "descriptions")
    except KeyError:
        descriptions_map = {}
    for name, spec in sorted(config.checks.specs.items()):
        (enabled if spec.enabled else disabled).append(name)
        descriptions[name] = descriptions_map.get(name, "")

    status = "FAIL" if any(
        v.severity == "error" for tr in table_reports for v in tr.violations
    ) else "PASS"

    n_err = sum(1 for tr in table_reports for v in tr.violations if v.severity == "error")
    n_warn = sum(1 for tr in table_reports for v in tr.violations if v.severity == "warning")
    if status == "FAIL":
        n_failing_tables = sum(
            1 for tr in table_reports if any(v.severity == "error" for v in tr.violations)
        )
        reason = f"{n_err} error(s) across {n_failing_tables} table(s)"
    elif n_warn:
        reason = f"all checks clean ({n_warn} warning(s))"
    else:
        reason = "all checks clean"

    target = None
    if target_config is not None:
        target = {"name": target_config.name, "description": target_config.description}

    return RunMetadata(
        epic=epic,
        generated_at=generated_at,
        duration_ms=duration_ms,
        status=status,
        status_reason=reason,
        tool_version=_tool_version,
        target=target,
        checks_enabled=enabled,
        checks_disabled=disabled,
        checks_descriptions=descriptions,
        contracts={tr.table: tr.contract_version for tr in table_reports},
        types_yaml_path=str(types_path),
        cli_args=list(sys.argv[1:]),
    )


# ---------------------------------------------------------------------------
# Rejected rows (row-centric aggregation)
# ---------------------------------------------------------------------------


def build_rejected_rows(
    *,
    tr: TableReport,
    eager_df,
    contract: Contract,
    cap: int,
    type_registry,
) -> None:
    """Populate `tr.rejected_rows` from the violations + eager df.

    Row-centric: aggregates all violations affecting the same (source_file,
    source_row) into a single RejectedRow. Carries the full source-row data
    (every contract field's value) so the report shows the surrounding
    columns, not just the offending field.
    """
    from dq_core.report.dimensions import dimension_for
    from dq_core.report.hints import hint_for

    # Group violations by (source_file, source_row); keep only those with both.
    grouped: dict[tuple[str, int], list[Violation]] = {}
    for v in tr.violations:
        if v.source_file and v.source_row is not None:
            grouped.setdefault((v.source_file, v.source_row), []).append(v)
    if not grouped:
        return

    def _worst_sev(vs: list[Violation]) -> int:
        return min(_SEVERITY_RANK.get(v.severity, 9) for v in vs)

    sorted_keys = sorted(
        grouped,
        key=lambda k: (_worst_sev(grouped[k]), k[0], k[1]),
    )
    truncated = max(len(sorted_keys) - cap, 0)
    sorted_keys = sorted_keys[:cap]
    tr.rejected_rows_truncated = truncated

    if "__source_file__" in eager_df.columns and "__row_index__" in eager_df.columns:
        rows = eager_df.to_dicts()
        lookup = {(r["__source_file__"], int(r["__row_index__"])): r for r in rows}
    else:
        lookup = {}

    contract_field_names = [f.name for f in contract.fields]
    pk_names = tr.pk_fields

    for key in sorted_keys:
        src_file, src_row = key
        viols = grouped[key]
        row_data_full = lookup.get(key, {})
        source_row_data = {
            name: row_data_full.get(name) for name in contract_field_names
        }
        pk_values = {name: row_data_full.get(name) for name in pk_names}

        rendered: list[dict[str, Any]] = []
        for v in viols:
            try:
                dim = dimension_for(v.kind).value
            except KeyError:
                dim = "unknown"
            try:
                hint = hint_for(v.kind)
            except KeyError:
                hint = ""
            phys: str | None = None
            if type_registry is not None and v.field:
                fc = next((f for f in contract.fields if f.name == v.field), None)
                if fc is not None:
                    try:
                        phys = type_registry.physical_type_for(fc)
                    except Exception:
                        phys = None
            rendered.append({
                "check_id": (
                    f"{tr.table}.{v.field}.{v.kind}" if v.field
                    else f"{tr.table}.{v.kind}"
                ),
                "kind": v.kind,
                "dimension": dim,
                "severity": v.severity,
                "field": v.field,
                "physical_type": phys,
                "offending_value": v.offending_value,
                "expected": v.expected,
                "hint": hint,
            })
        worst = min(viols, key=lambda v: _SEVERITY_RANK.get(v.severity, 9)).severity

        tr.rejected_rows.append(RejectedRow(
            source_file=src_file,
            source_row=int(src_row),
            pk_values=pk_values,
            source_row_data=source_row_data,
            violations=rendered,
            worst_severity=worst,
        ))
