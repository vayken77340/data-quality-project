"""Markdown summary — fits in a PR comment or Slack message (<= 50 lines)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from data_contract.validate_data.runner import TableReport, ValidationReport


TOP_N = 5
LINE_CAP = 60  # soft cap


def render_markdown(report: ValidationReport) -> str:
    counts = report.summary_counts
    status = "**FAIL**" if report.has_errors else "**PASS**"
    lines: list[str] = []
    lines.append(f"# Data quality report - epic {report.epic} - {report.generated_at}")
    lines.append("")
    lines.append(
        f"Overall: {status} ({counts['error']} errors, {counts['warning']} warnings)"
    )
    lines.append("")
    lines.append("## Tables")

    for tr in report.table_reports:
        lines.append("")
        lines.extend(_render_table(tr))
        if len(lines) >= LINE_CAP:
            lines.append("")
            lines.append(f"... output truncated at {LINE_CAP} lines; see the XLSX/JSON for the full report.")
            break

    return "\n".join(lines) + "\n"


def write_markdown(report: ValidationReport, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(report), encoding="utf-8")
    return out_path


def _render_table(tr: TableReport) -> list[str]:
    errors = [v for v in tr.violations if v.severity == "error"]
    warnings = [v for v in tr.violations if v.severity == "warning"]
    n_violations = len(errors) + len(warnings)

    if not n_violations:
        return [f"### {tr.table} - clean ({tr.total_rows} rows, {len(tr.input_files)} files)"]

    rate = (tr.total_rows - n_violations) / tr.total_rows if tr.total_rows else 0.0
    out: list[str] = [
        f"### {tr.table} - {rate*100:.2f}% clean ({n_violations} violations / {tr.total_rows} rows)"
    ]

    # Top violations by (kind, field).
    counter: Counter = Counter()
    for v in tr.violations:
        counter[(v.severity, v.kind, v.field)] += 1
    for (severity, kind, field), n in counter.most_common(TOP_N):
        target = f" on `{field}`" if field else ""
        out.append(f"- {n} `{kind}` {severity}s{target}")
    return out
