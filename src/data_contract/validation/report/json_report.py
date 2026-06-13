"""JSON report -- structured machine-readable output for engineers / dashboards.

Top-level shape (v1):

    {
      "schema_version": "1.0",
      "run":     { ...run metadata block... },
      "summary": { pass, score, by_severity, by_dimension },
      "tables":  [ { table, score, by_dimension, input, violations, profile, rejected_rows } ],
      "run_issues": [ ... ]
    }

Violations are aggregated per (kind, field) with up to `rejected_row_cap`
sample offending values. PK-not-unique violations are clustered into a single
entry per field listing every duplicate value + its occurrences. The
rejected_rows array carries the row-centric view with FULL source-row context
(every contract field's value on the offending row) so engineers can see the
surrounding columns without grepping the source file.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from data_contract.contract import Contract
from data_contract.validation.report.aggregation import aggregate_top_values
from data_contract.validation.report.dimensions import (
    Dimension,
    QUALITY_DIMENSIONS,
    compute_overall_score,
    dimension_for,
    violation_kind_for_check,
)
from data_contract.validation.report.hints import hint_for
from data_contract.validation.report.strings import load_strings
from data_contract.validation.runner import (
    RejectedRow,
    TableReport,
    ValidationReport,
)
from data_contract.violations import Violation


_TOP_VALUES_N = 10


def _check_label_for(kind: str, field_type_label: str = "") -> str:
    """Resolve the business-friendly label for a violation kind.

    Mirrors `xlsx._check_label` so JSON/HTML/Markdown/Excel all surface the
    same wording. `{type}` placeholder is substituted from the caller-supplied
    physical type label; when missing, the templated suffix is dropped so we
    never render a dangling "Type violation: " with an empty type.
    """
    try:
        labels = load_strings().get("rejected_sheet", "check_labels")
    except KeyError:
        labels = {}
    template = labels.get(kind, kind)
    if "{type}" in template:
        if field_type_label:
            return template.format(type=field_type_label)
        return template.split("{type}")[0].rstrip(": ").strip()
    return template


SCHEMA_VERSION = "1.0"


def render_json(report: ValidationReport, contracts_by_table: dict[str, Contract]) -> dict[str, Any]:
    counts = report.summary_counts
    table_payloads = [_render_table(tr, report.settings.rejected_row_cap) for tr in report.table_reports]
    overall = compute_overall_score([(tr.total_rows, tr.score) for tr in report.table_reports if tr.score])

    summary = {
        "pass": not report.has_errors,
        "score": overall.score,
        "by_severity": counts,
        "by_dimension": {
            d.value: {
                "score": overall.by_dimension[d].score,
                "violations": overall.by_dimension[d].violations,
                "affected_rows": overall.by_dimension[d].affected_rows,
            }
            for d in QUALITY_DIMENSIONS
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "run": _render_run(report),
        "summary": summary,
        "tables": table_payloads,
        "run_issues": _render_run_issues(report),
    }


def write_json(report: ValidationReport, contracts_by_table: dict[str, Contract], out_path: Path) -> Path:
    payload = render_json(report, contracts_by_table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    return out_path


def _json_default(o: Any) -> Any:
    # Polars / Arrow may emit values like Date / Datetime / Decimal.
    return str(o)


# ---------------------------------------------------------------------------
# Run metadata block
# ---------------------------------------------------------------------------


def _render_run(report: ValidationReport) -> dict[str, Any]:
    rm = report.run_metadata
    if rm is None:
        return {
            "epic": report.epic, "generated_at": report.generated_at,
            "duration_ms": 0, "status": "FAIL" if report.has_errors else "PASS",
            "status_reason": "", "tool_version": "", "target": None,
            "checks": {"enabled": [], "disabled": [], "active": []},
            "contracts": {}, "types_yaml_path": "", "cli_args": [],
        }
    # `active` lists enabled checks with their YAML-supplied descriptions and
    # the violation kind each emits. Drives the report's Checks section.
    active = []
    for name in rm.checks_enabled:
        try:
            vk = violation_kind_for_check(name)
        except KeyError:
            vk = ""
        active.append({
            "name": name,
            "description": rm.checks_descriptions.get(name, ""),
            "violation_kind": vk,
        })
    return {
        "epic": rm.epic,
        "generated_at": rm.generated_at,
        "duration_ms": rm.duration_ms,
        "status": rm.status,
        "status_reason": rm.status_reason,
        "tool_version": rm.tool_version,
        "target": rm.target,
        "checks": {
            "enabled": rm.checks_enabled,
            "disabled": rm.checks_disabled,
            "active": active,
        },
        "contracts": rm.contracts,
        "types_yaml_path": rm.types_yaml_path,
        "cli_args": rm.cli_args,
    }


# ---------------------------------------------------------------------------
# Per-table payload
# ---------------------------------------------------------------------------


def _render_table(tr: TableReport, sample_cap: int) -> dict[str, Any]:
    score = tr.score
    payload: dict[str, Any] = {
        "table": tr.table,
        "contract_version": tr.contract_version,
        "pk_fields": list(tr.pk_fields),
        "input": {
            "files": [{"path": str(p.name), "rows": rows} for p, rows in tr.input_files],
            "total_rows": tr.total_rows,
            "clean_rows": score.clean_rows if score else tr.total_rows,
            "affected_rows": score.affected_rows if score else 0,
        },
        "score": score.score if score else 100.0,
        "by_dimension": (
            {
                d.value: {
                    "score": score.by_dimension[d].score,
                    "violations": score.by_dimension[d].violations,
                    "affected_rows": score.by_dimension[d].affected_rows,
                }
                for d in QUALITY_DIMENSIONS
            }
            if score else {}
        ),
        "by_check": _render_by_check(tr.by_check),
        "metrics": _render_metrics(tr.metrics),
        "violations": (
            _cluster_pk_violations(tr.violations, tr.pk_fields, sample_cap)
            + _condense_other_violations(tr.violations, tr.table, sample_cap)
        ),
        "profile": _render_profile(tr.profile, tr.metrics),
        "rejected_rows": [_render_rejected_row(r) for r in tr.rejected_rows],
        "rejected_rows_truncated": tr.rejected_rows_truncated,
    }
    return payload


def _render_by_check(by_check: dict) -> dict[str, dict[str, Any]]:
    """Render the per-(table, check) status map for JSON consumers.

    Empty dict (the dataclass default) means by_check wasn't populated --
    typically a partial test fixture; just emit `{}` to keep the schema
    stable.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, status in by_check.items():
        out[name] = {
            "status": status.status,
            "scope": status.scope,
            "pass_rows": status.pass_rows,
            "warning_rows": status.warning_rows,
            "error_rows": status.error_rows,
            "violation_count": status.violation_count,
        }
    return out


def _render_metrics(metrics: dict) -> dict[str, dict[str, Any]]:
    """Render configurable metrics for JSON consumers."""
    out: dict[str, dict[str, Any]] = {}
    for name, result in metrics.items():
        out[name] = {
            "scope": result.scope,
            "values": dict(result.values),
        }
    return out


def _render_profile(profile, metrics: dict) -> dict[str, Any] | None:
    """Render the Profile block as field metadata + the three null/distinct
    columns sourced from `tr.metrics`. When a metric is disabled, that
    column is `None` (renderers display "--").
    """
    if profile is None:
        return None
    null_count = (metrics.get("null_count").values
                  if "null_count" in metrics else {})
    null_pct = (metrics.get("null_percentage").values
                if "null_percentage" in metrics else {})
    distinct = (metrics.get("distinct_count").values
                if "distinct_count" in metrics else {})
    return {
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "type_format": f.type_format,
                "is_primary_key": f.is_pk,
                "is_foreign_key": f.is_fk,
                "total": f.total,
                "null_count": null_count.get(f.name),
                "null_pct": null_pct.get(f.name),
                "distinct_count": distinct.get(f.name),
            }
            for f in profile.fields
        ]
    }


def _render_rejected_row(r: RejectedRow) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    for v in r.violations:
        out = dict(v)
        # Friendly label for the Check column / HTML header. Type-templated
        # kinds (type_coercion_violation) carry the per-row physical type that
        # the runner attached, so the label reads "Type violation: integer".
        out["check_label"] = _check_label_for(
            v.get("kind") or "", v.get("physical_type") or ""
        )
        enriched.append(out)
    return {
        "source_file": r.source_file,
        "source_row": r.source_row,
        "pk_values": dict(r.pk_values),
        "source_row_data": dict(r.source_row_data),
        "worst_severity": r.worst_severity,
        "violations": enriched,
    }


# ---------------------------------------------------------------------------
# Violation aggregation
# ---------------------------------------------------------------------------


def _check_id(table: str, field: str | None, kind: str) -> str:
    return f"{table}.{field}.{kind}" if field else f"{table}.{kind}"


def _cluster_pk_violations(
    violations: list[Violation], pk_fields: list[str], sample_cap: int,
) -> list[dict[str, Any]]:
    """Group `pk_not_unique` violations into one entry per (table, PK)."""
    if not pk_fields:
        return []
    clusters: dict[tuple, list[Violation]] = defaultdict(list)
    for v in violations:
        if v.kind != "pk_not_unique" or v.pk_values is None:
            continue
        key = tuple(v.pk_values.get(c) for c in pk_fields)
        clusters[key].append(v)
    if not clusters:
        return []

    duplicates = []
    total_rows = 0
    spans = set()
    # PK-level top values: each duplicate cluster IS a (value, count) pair, so
    # we synthesize the TopValues directly off the cluster sizes rather than
    # routing back through `aggregate_top_values` (which keys on
    # `offending_value`, irrelevant for multi-column PKs).
    cluster_sizes: list[tuple[str, int]] = []
    for key, participants in clusters.items():
        value_repr = (
            ", ".join(f"{c}={v}" for c, v in zip(pk_fields, key))
            if len(pk_fields) > 1
            else str(key[0])
        )
        cluster_sizes.append((value_repr, len(participants)))
        duplicates.append({
            "value": list(key) if len(key) > 1 else key[0],
            "occurrences": [
                {"source_file": p.source_file, "source_row": p.source_row}
                for p in participants
            ],
        })
        total_rows += len(participants)
        for p in participants:
            if p.source_file:
                spans.add(p.source_file)

    cluster_sizes.sort(key=lambda t: -t[1])
    top = cluster_sizes[:_TOP_VALUES_N]
    remainder = cluster_sizes[_TOP_VALUES_N:]
    top_values = [{"value": v, "count": c} for v, c in top]
    remaining_rows = sum(c for _, c in remainder)
    remaining_distinct = len(remainder)

    table = clusters[next(iter(clusters))][0].table
    field = ", ".join(pk_fields) if len(pk_fields) > 1 else pk_fields[0]
    return [{
        "check_id": _check_id(table, ", ".join(pk_fields), "pk_not_unique"),
        "kind": "pk_not_unique",
        "check_label": _check_label_for("pk_not_unique"),
        "dimension": dimension_for("pk_not_unique").value,
        "severity": "error",
        "field": field,
        "row_count": total_rows,
        "distinct_values": len(clusters),
        "spans_files": len(spans),
        "expected": "must be unique",
        "hint": hint_for("pk_not_unique"),
        "top_values": top_values,
        "remaining_rows": remaining_rows,
        "remaining_distinct": remaining_distinct,
        "duplicates": duplicates,
    }]


def _condense_other_violations(
    violations: list[Violation], table: str, sample_cap: int,
) -> list[dict[str, Any]]:
    by_kind_field: dict[tuple, list[Violation]] = defaultdict(list)
    for v in violations:
        if v.kind == "pk_not_unique":
            continue
        # Table-level violations (no source row) belong in the Table issues
        # section, not in Top issues. Keeping them here would mix
        # "5x missing column" with "300x type violation on row 7" and water
        # down the row-level signal.
        if _is_table_level(v):
            continue
        by_kind_field[(v.kind, v.field)].append(v)

    out: list[dict[str, Any]] = []
    for (kind, field), bucket in by_kind_field.items():
        try:
            dim = dimension_for(kind).value
        except KeyError:
            dim = "unknown"
        try:
            hint = hint_for(kind)
        except KeyError:
            hint = ""
        agg = aggregate_top_values(bucket, n=_TOP_VALUES_N)
        out.append({
            "check_id": _check_id(table, field, kind),
            "kind": kind,
            "check_label": _check_label_for(kind),
            "dimension": dim,
            "severity": bucket[0].severity,
            "field": field,
            "row_count": len(bucket),
            "expected": bucket[0].expected,
            "hint": hint,
            "top_values": [
                {"value": value, "count": count} for value, count in agg.top
            ],
            "remaining_rows": agg.remaining_rows,
            "remaining_distinct": agg.remaining_distinct,
            "sample_offending_values": [
                {
                    "value": v.offending_value,
                    "source_file": v.source_file,
                    "source_row": v.source_row,
                }
                for v in bucket[:sample_cap]
            ],
        })
    return out


_SEVERITY_RANK_JSON = {"error": 0, "warning": 1, "info": 2}


def _render_run_issues(report: ValidationReport) -> list[dict[str, Any]]:
    """Table-level issues -- every violation that has no source row.

    Covers schema gaps (`column_missing`, `extra_column`) AND operational
    failures (`no_input_files`, `parser_failure`, `fk_target_table_not_loaded`).
    Row-keyed violations live in `tables[].rejected_rows`; this list is the
    catch-all for everything else so nothing slips through the cracks.

    JSON key is `run_issues` for backwards compatibility; the user-facing
    label across HTML/MD/XLSX is "Table issues".
    """
    issues: list[dict[str, Any]] = []
    for tr in report.table_reports:
        for v in tr.violations:
            if not _is_table_level(v):
                continue
            try:
                hint = hint_for(v.kind)
            except KeyError:
                hint = ""
            try:
                dim = dimension_for(v.kind).value
            except KeyError:
                dim = "unknown"
            issues.append({
                "table": v.table,
                "kind": v.kind,
                "check_label": _check_label_for(v.kind),
                "dimension": dim,
                "severity": v.severity,
                "field": v.field,
                "expected": v.expected,
                "offending_value": v.offending_value,
                "hint": hint,
            })
    # Errors before warnings before info, then by table+kind for stable output.
    issues.sort(key=lambda i: (
        _SEVERITY_RANK_JSON.get(i["severity"], 9),
        i["table"] or "",
        i["kind"],
        i.get("field") or "",
    ))
    return issues


def _is_table_level(v: Violation) -> bool:
    """A violation is table-level when it has no specific source row.

    Schema gaps (column_missing, extra_column) and operational failures
    (no_input_files, parser_failure, fk_target_table_not_loaded) all fit
    this definition. Row-keyed violations have a (source_file, source_row)
    and surface via `rejected_rows`.
    """
    return v.source_row is None
