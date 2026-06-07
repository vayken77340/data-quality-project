"""JSON report — machine-readable detailed output for engineers / dashboards.

Clusters PK-not-unique violations (one entry per duplicated value, with all
participants in `occurrences`), caps sample offending values, and provides
per-table summaries.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from data_contract.contract import Contract
from data_contract.validate_data.runner import TableReport, ValidationReport
from data_contract.validate_data.violations import Violation


SAMPLE_CAP = 10


def render_json(report: ValidationReport, contracts_by_table: dict[str, Contract]) -> dict[str, Any]:
    counts = report.summary_counts
    return {
        "epic": report.epic,
        "generated_at": report.generated_at,
        "summary": {
            "pass": not report.has_errors,
            "errors": counts["error"],
            "warnings": counts["warning"],
            "info": counts["info"],
        },
        "tables": [_render_table(tr, contracts_by_table[tr.table]) for tr in report.table_reports],
    }


def write_json(report: ValidationReport, contracts_by_table: dict[str, Contract], out_path: Path) -> Path:
    payload = render_json(report, contracts_by_table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    return out_path


def _json_default(o: Any) -> Any:
    # Polars / Arrow may emit values like Date / Datetime / Decimal.
    return str(o)


def _render_table(tr: TableReport, contract: Contract) -> dict[str, Any]:
    table_violations = list(tr.violations)
    pk_clusters = _cluster_pk_violations(table_violations, tr.pk_fields)
    other = _condense_other_violations(table_violations)
    return {
        "table": tr.table,
        "contract_version": tr.contract_version,
        "pk_fields": list(tr.pk_fields),
        "input": {
            "files": [{"path": str(p.name), "rows": rows} for p, rows in tr.input_files],
            "total_rows": tr.total_rows,
        },
        "violations": pk_clusters + other,
    }


def _cluster_pk_violations(
    violations: list[Violation], pk_fields: list[str],
) -> list[dict[str, Any]]:
    """Group `pk_not_unique` violations by their PK tuple."""
    if not pk_fields:
        return []
    clusters: dict[tuple, list[Violation]] = defaultdict(list)
    for v in violations:
        if v.kind != "pk_not_unique":
            continue
        if v.pk_values is None:
            continue
        key = tuple(v.pk_values.get(c) for c in pk_fields)
        clusters[key].append(v)

    if not clusters:
        return []

    out = []
    for key, participants in clusters.items():
        spans = {p.source_file for p in participants if p.source_file}
        out.append({
            "kind": "pk_not_unique",
            "severity": "error",
            "field": ", ".join(pk_fields) if len(pk_fields) > 1 else pk_fields[0],
            "row_count": len(participants),
            "distinct_values": 1,
            "spans_files": len(spans),
            "duplicates": [{
                "value": list(key) if len(key) > 1 else key[0],
                "occurrences": [
                    {"source_file": p.source_file, "source_row": p.source_row}
                    for p in participants
                ],
            }],
        })
    # Aggregate distinct_values + row_count across all clusters into one block
    # since the JSON wants a single `pk_not_unique` entry per field.
    aggregated = {
        "kind": "pk_not_unique",
        "severity": "error",
        "field": ", ".join(pk_fields) if len(pk_fields) > 1 else pk_fields[0],
        "row_count": sum(e["row_count"] for e in out),
        "distinct_values": len(out),
        "spans_files": max((e["spans_files"] for e in out), default=0),
        "duplicates": [d for e in out for d in e["duplicates"]],
    }
    return [aggregated]


def _condense_other_violations(violations: list[Violation]) -> list[dict[str, Any]]:
    by_kind_field: dict[tuple, list[Violation]] = defaultdict(list)
    for v in violations:
        if v.kind == "pk_not_unique":
            continue
        key = (v.kind, v.field)
        by_kind_field[key].append(v)

    out = []
    for (kind, field), bucket in by_kind_field.items():
        out.append({
            "kind": kind,
            "severity": bucket[0].severity,
            "field": field,
            "row_count": len(bucket),
            "expected": bucket[0].expected,
            "sample_offending_values": [
                {
                    "value": v.offending_value,
                    "source_file": v.source_file,
                    "source_row": v.source_row,
                }
                for v in bucket[:SAMPLE_CAP]
            ],
        })
    return out
