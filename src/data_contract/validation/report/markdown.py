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

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_contract.validation.report.aggregation import aggregate_top_values
from data_contract.validation.report.dimensions import (
    QUALITY_DIMENSIONS,
    compute_overall_score,
)
from data_contract.validation.report.hints import hint_for
from data_contract.validation.report.strings import load_strings
from data_contract.validation.runner import ValidationReport
from data_contract.violations import Violation


TOP_N = 5
_SAMPLE_N = 3                                   # number of offending values shown inline


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

    # Check status grid: tables x checks, status + counts inline.
    if any(tr.by_check for tr in report.table_reports):
        _render_check_status_section(lines, report, S)

    # Metrics: per-table compact field x metric table.
    if any(tr.metrics for tr in report.table_reports):
        _render_metrics_section(lines, report, S)

    # Table-level issues (schema gaps + operational failures) -- surfaced
    # before Top issues so a missing column / unreadable file is the first
    # thing the reader sees.
    table_issues = _table_level_issues(report)
    check_labels = S.get("rejected_sheet", "check_labels")
    if table_issues:
        lines.append(S.get("markdown", "table_issues_section"))
        lines.append("")
        for ti in table_issues:
            check = check_labels.get(ti.kind, ti.kind)
            field_str = f" on `{ti.field}`" if ti.field else ""
            lines.append(
                f"- **{ti.severity.upper()}** `{ti.table}` - {check}"
                f"{field_str}: {ti.expected}"
            )
        lines.append("")

    # Top issues with hints + offending-value context.
    lines.append(S.get("markdown", "top_issues_section"))
    lines.append("")
    top = _top_violations(report)
    if not top:
        lines.append(S.get("markdown", "no_issues"))
    else:
        details = S.get("markdown", "issue_details")
        for issue in top:
            try:
                hint = hint_for(issue.kind)
            except KeyError:
                hint = ""
            kind_label = _kind_label(issue, check_labels)
            detail = _render_detail(issue, details)
            tmpl_key = "issue_with_field_template" if issue.field else "issue_without_field_template"
            lines.append(S.fmt(
                "markdown", tmpl_key,
                count=issue.count, kind_label=kind_label,
                field=issue.field or "", hint=hint, detail=detail,
            ))
    lines.append("")
    lines.append(S.get("markdown", "footer"))

    return "\n".join(lines) + "\n"


def write_markdown(report: ValidationReport, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(report), encoding="utf-8")
    return out_path


def _render_check_status_section(lines: list[str], report: ValidationReport, S) -> None:
    """Append the tables x checks grid to `lines`."""
    SL = S.get("check_status_sheet", "status_labels")
    cell_tpl = S.get("check_status_sheet", "cell_template_row")
    cell_tpl_table = S.get("check_status_sheet", "cell_template_table")
    skipped_label = SL.get("SKIPPED", "--")
    na_label = SL.get("NA", "n/a")

    check_names: set[str] = set()
    for tr in report.table_reports:
        check_names.update(tr.by_check.keys())
    sorted_checks = sorted(check_names)
    if not sorted_checks:
        return

    lines.append(S.get("markdown", "check_status_section"))
    lines.append("")
    lines.append("| Table | " + " | ".join(sorted_checks) + " |")
    lines.append("|" + "|".join(["---"] * (len(sorted_checks) + 1)) + "|")
    for tr in report.table_reports:
        cells: list[str] = [tr.table]
        for check in sorted_checks:
            status = tr.by_check.get(check)
            if status is None:
                cells.append("")
                continue
            label = SL.get(status.status, status.status)
            if status.status == "SKIPPED":
                cells.append(skipped_label)
            elif status.status == "NA":
                cells.append(na_label)
            elif status.scope == "table":
                cells.append(cell_tpl_table.format(
                    label=label,
                    count=status.violation_count,
                ))
            else:
                cells.append(cell_tpl.format(
                    label=label,
                    pass_rows=status.pass_rows if status.pass_rows is not None else 0,
                    warning_rows=status.warning_rows,
                    error_rows=status.error_rows,
                ))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")


def _render_metrics_section(lines: list[str], report: ValidationReport, S) -> None:
    """Append per-table field x metric tables to `lines`."""
    metric_labels = S.get("metrics_sheet", "metric_labels")
    lines.append(S.get("markdown", "metrics_section"))
    lines.append("")
    for tr in report.table_reports:
        if not tr.metrics:
            continue
        lines.append(f"### {tr.table}")
        lines.append("")
        table_metrics = sorted(
            (n, r) for n, r in tr.metrics.items() if r.scope == "table"
        )
        if table_metrics:
            for name, result in table_metrics:
                label = metric_labels.get(name, name)
                value = result.values.get("__table__")
                lines.append(f"- **{label}**: {value}")
            lines.append("")
        field_metrics = sorted(
            (n, r) for n, r in tr.metrics.items() if r.scope == "field"
        )
        if not field_metrics:
            continue
        headers = ["Field"] + [metric_labels.get(n, n) for n, _ in field_metrics]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        field_names: list[str] = []
        seen: set[str] = set()
        for _, result in field_metrics:
            for fname in result.values:
                if fname == "__table__" or fname in seen:
                    continue
                seen.add(fname)
                field_names.append(fname)
        field_names.sort()
        for fname in field_names:
            cells: list[str] = [fname]
            for _, result in field_metrics:
                val = result.values.get(fname, "")
                if isinstance(val, float):
                    cells.append(f"{val:.2f}")
                else:
                    cells.append(str(val))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")


@dataclass(frozen=True)
class _Issue:
    kind: str
    field: str | None
    count: int
    violations: tuple[Violation, ...]


@dataclass(frozen=True)
class _TableIssue:
    table: str
    kind: str
    severity: str
    field: str | None
    expected: str


_SEV_RANK_MD = {"error": 0, "warning": 1, "info": 2}


def _table_level_issues(report: ValidationReport) -> list[_TableIssue]:
    """Collect schema gaps + operational failures (no source row) sorted by
    severity, then table+kind for stable output."""
    out: list[_TableIssue] = []
    for tr in report.table_reports:
        for v in tr.violations:
            if v.source_row is not None:
                continue
            out.append(_TableIssue(
                table=v.table or "", kind=v.kind, severity=v.severity or "",
                field=v.field, expected=v.expected or "",
            ))
    out.sort(key=lambda i: (
        _SEV_RANK_MD.get(i.severity, 9), i.table, i.kind, i.field or "",
    ))
    return out


def _top_violations(report: ValidationReport) -> list[_Issue]:
    """Aggregate row-level violations by (kind, field) and return the TOP_N
    largest groups. Table-level kinds (schema gaps, operational failures)
    are excluded -- they have their own section so they don't compete with
    row counts here.
    """
    grouped: dict[tuple[str, str | None], list[Violation]] = defaultdict(list)
    for tr in report.table_reports:
        for v in tr.violations:
            if v.source_row is None:
                continue
            grouped[(v.kind, v.field)].append(v)
    sorted_groups = sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:TOP_N]
    return [
        _Issue(kind=kind, field=field, count=len(violations),
               violations=tuple(violations))
        for (kind, field), violations in sorted_groups
    ]


def _kind_label(issue: _Issue, check_labels: dict[str, str]) -> str:
    """Resolve the business-friendly label for a violation kind.

    `type_coercion_violation` carries a `{type}` placeholder which we leave
    in literal form here (the markdown is summary text -- the type would
    repeat for each row in the group). Fall back to the raw kind string
    if no label exists.
    """
    template = check_labels.get(issue.kind, issue.kind)
    if "{type}" in template:
        # Strip the templated suffix for the headline -- the per-row type
        # variation is surfaced in the rejected sheet, not the summary.
        return template.split("{type}")[0].rstrip(": ").strip()
    return template


def _render_detail(issue: _Issue, details: dict[str, str]) -> str:
    """Render the per-kind detail block (limit + longest, sample values, etc.).

    Returns an empty string when no detail template is configured for this
    kind, so the issue line collapses to just the headline + hint.
    """
    template = details.get(issue.kind, "")
    if not template:
        return ""
    kwargs = _detail_kwargs(issue)
    try:
        return template.format(**kwargs)
    except KeyError:
        # Template referenced a placeholder we didn't compute -- skip the
        # detail rather than crash the whole report.
        return ""


def _detail_kwargs(issue: _Issue) -> dict[str, Any]:
    """Compute the placeholder values consumed by each kind's detail template."""
    samples = _top_value_samples(issue.violations, n=_SAMPLE_N)
    kwargs: dict[str, Any] = {"sample": ", ".join(samples)}
    if issue.kind == "max_length_violation":
        kwargs["limit"] = _max_length_limit(issue.violations)
        kwargs["longest"] = _longest_value_length(issue.violations)
    if issue.kind == "pk_not_unique":
        # PK violations carry the offending PK in `pk_values`; surface that
        # instead of the raw cell value (which is just one PK column).
        first_pk = next(
            (v.pk_values for v in issue.violations if v.pk_values), None
        )
        kwargs["sample_pk"] = (
            ", ".join(f"{k}={_fmt_value(v)}" for k, v in first_pk.items())
            if first_pk else "?"
        )
    return kwargs


def _top_value_samples(violations: tuple[Violation, ...], *, n: int) -> list[str]:
    """Pick the `n` most-frequent non-null offending values, formatted for MD.

    Driven by the shared `aggregate_top_values` helper so the MD samples line
    up with the HTML top-N drill-down. Null values are dropped here (the kind
    is implied -- `nullable_violation` has no sample line at all).
    """
    agg = aggregate_top_values(violations, n=n + 1)   # +1 to absorb a possible null bucket
    out: list[str] = []
    for value, count in agg.top:
        if value == "(null)":
            continue
        out.append(f"`{value}` ({count})")
        if len(out) >= n:
            break
    return out


def _max_length_limit(violations: tuple[Violation, ...]) -> str:
    """Extract the contract's max_length from any violation's `expected` field.

    The expected text is shaped `"max {n} chars"` -- a small regex pulls the
    limit out so we don't need to thread the contract through the markdown
    renderer.
    """
    for v in violations:
        m = re.search(r"max\s+(\d+)\s+chars", v.expected or "")
        if m:
            return m.group(1)
    return "?"


def _longest_value_length(violations: tuple[Violation, ...]) -> int:
    """Longest character length observed across the group's offending values."""
    longest = 0
    for v in violations:
        val = v.offending_value
        if isinstance(val, str) and len(val) > longest:
            longest = len(val)
    return longest


def _fmt_value(value: Any) -> str:
    """Render an offending value for a markdown summary cell.

    Strings get surrounded by backticks and truncated; non-strings are repr'd.
    """
    if isinstance(value, str):
        if len(value) > 50:
            return f"`{value[:47]}...`"
        return f"`{value}`"
    return f"`{value!r}`"
