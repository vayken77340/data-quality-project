"""Markdown summary -- fits a PR comment / Slack message.

Engineer-facing concise format. Carries the status banner, overall score,
per-dimension scores, per-table summary, top-N issues (with action hints),
and a footer surfacing the target and any disabled checks.

Full evidence (rejected rows with source context, data profile, complete
violation list) lives in the JSON / HTML / XLSX files; this one is for the
quick glance.

Every user-facing string is sourced from `configs/report_strings.yaml` via
`report.strings.load_strings()`.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from data_contract.validate_data.report.dimensions import (
    QUALITY_DIMENSIONS,
    compute_overall_score,
)
from data_contract.validate_data.report.hints import hint_for
from data_contract.validate_data.report.strings import load_strings
from data_contract.validate_data.runner import ValidationReport


TOP_N = 5


def render_markdown(report: ValidationReport) -> str:
    S = load_strings()
    counts = report.summary_counts
    rm = report.run_metadata
    overall = compute_overall_score(
        [(tr.total_rows, tr.score) for tr in report.table_reports if tr.score]
    )

    status = S.get("markdown", "status_fail") if report.has_errors \
        else S.get("markdown", "status_pass")
    lines: list[str] = []
    lines.append(S.fmt("markdown", "title_template",
                       epic=report.epic, generated_at=report.generated_at))
    lines.append("")
    lines.append(S.fmt("markdown", "headline_template",
                       status=status, score=overall.score,
                       errors=counts["error"], warnings=counts["warning"]))
    if rm and rm.target:
        if rm.checks_disabled:
            lines.append(S.fmt(
                "markdown", "target_with_disabled_template",
                target_name=rm.target["name"],
                disabled_count=len(rm.checks_disabled),
                disabled_list=", ".join(rm.checks_disabled),
            ))
        else:
            lines.append(S.fmt("markdown", "target_template",
                               target_name=rm.target["name"]))
    elif rm and rm.checks_disabled:
        lines.append(S.fmt(
            "markdown", "no_target_with_disabled_template",
            disabled_count=len(rm.checks_disabled),
            disabled_list=", ".join(rm.checks_disabled),
        ))
    lines.append("")

    # Dimension table.
    lines.append(S.get("markdown", "dimensions_table_header"))
    lines.append(S.get("markdown", "dimensions_table_separator"))
    for d in QUALITY_DIMENSIONS:
        ds = overall.by_dimension[d]
        lines.append(S.fmt("markdown", "dimensions_row_template",
                           dimension=d.value.capitalize(),
                           score=ds.score, violations=ds.violations))
    lines.append("")

    # Per-table table.
    lines.append(S.get("markdown", "tables_section"))
    lines.append("")
    lines.append(S.get("markdown", "tables_table_header"))
    lines.append(S.get("markdown", "tables_table_separator"))
    for tr in report.table_reports:
        score = tr.score.score if tr.score else 100.0
        clean = tr.score.clean_rows if tr.score else tr.total_rows
        n_err = sum(1 for v in tr.violations if v.severity == "error")
        n_warn = sum(1 for v in tr.violations if v.severity == "warning")
        lines.append(S.fmt("markdown", "tables_row_template",
                           table=tr.table, score=score,
                           total_rows=tr.total_rows, clean=clean,
                           errors=n_err, warnings=n_warn))
    lines.append("")

    # Top issues with hints.
    lines.append(S.get("markdown", "top_issues_section"))
    lines.append("")
    top = _top_violations(report)
    if not top:
        lines.append(S.get("markdown", "no_issues"))
    else:
        for (kind, field, n) in top:
            try:
                hint = hint_for(kind)
            except KeyError:
                hint = ""
            if field:
                lines.append(S.fmt("markdown", "issue_with_field_template",
                                   count=n, kind=kind, field=field, hint=hint))
            else:
                lines.append(S.fmt("markdown", "issue_without_field_template",
                                   count=n, kind=kind, hint=hint))
    lines.append("")
    lines.append(S.get("markdown", "footer"))

    return "\n".join(lines) + "\n"


def write_markdown(report: ValidationReport, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(report), encoding="utf-8")
    return out_path


def _top_violations(report: ValidationReport) -> list[tuple[str, str | None, int]]:
    """Aggregate every violation (across tables) by (kind, field) and return
    the TOP_N most frequent ones."""
    counter: Counter = Counter()
    for tr in report.table_reports:
        for v in tr.violations:
            counter[(v.kind, v.field)] += 1
    out: list[tuple[str, str | None, int]] = []
    for (kind, field), n in counter.most_common(TOP_N):
        out.append((kind, field, n))
    return out
