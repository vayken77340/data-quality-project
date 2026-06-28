"""XLSX report -- business-facing workbook.

Sheets (in this order):
  Run               - run metadata (epic, target, status, checks, versions)
  Summary           - overall block + per-table scorecard with inline per-dimension scores
  Profile           - one section per table (heading + field stats), separated by blank rows
  Checks            - enabled checks reference: knob -> description -> emitted violation kind
  <table>_rejected  - one row per violation, identified by Source File + PK columns
                      (or Source Row if no PK). One column per contract field carries
                      the violating value (only the field this violation is on is
                      filled). Check + Expected describe the rule that was broken.
  Run issues        - operational failures (only if any)
  Check status      - tables x checks status grid (only if populated)
  Metrics           - per-table metrics sections (only if populated)

Every user-facing string is sourced from `configs/report_strings.yaml` via
`report.strings.load_strings()`. The rejected sheet honours
`config.settings.rejected_row_cap` and appends a truncation marker row when
over cap.

`write_xlsx` is the only public symbol. Sheet builders live in sibling
modules so this file stays a thin top-level orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from dq_core.contract import Contract
from dq_core.report_models import ValidationReport
from dq_core.report.strings import load_strings
from dq_core.report.xlsx import (
    check_status_sheet,
    checks_sheet,
    metrics_sheet,
    profile_sheet,
    rejected_sheet,
    run_issues_sheet,
    run_sheet,
    summary_sheet,
)
from dq_core.report.xlsx._common import (
    has_run_issues, safe_sheet_name,
)


def write_xlsx(
    report: ValidationReport,
    contracts_by_table: dict[str, Contract],
    out_path: Path,
) -> Path:
    S = load_strings()
    wb = Workbook()
    # The initial active sheet becomes the Run sheet -- rename it first.
    run_ws = wb.active
    run_ws.title = S.get("sheets", "run")
    run_sheet.build(run_ws, report, S)

    summary_ws = wb.create_sheet(S.get("sheets", "summary"))
    summary_sheet.build(summary_ws, report, S)

    if any(tr.profile is not None for tr in report.table_reports):
        profile_ws = wb.create_sheet(S.get("sheets", "profile"))
        profile_sheet.build(profile_ws, report, S)

    checks_ws = wb.create_sheet(S.get("sheets", "checks"))
    checks_sheet.build(checks_ws, report, S)

    for tr in report.table_reports:
        if tr.rejected_rows:
            sheet_name = safe_sheet_name(
                S.fmt("sheets", "rejected_pattern", table=tr.table),
            )
            ws = wb.create_sheet(sheet_name)
            rejected_sheet.build(ws, tr, contracts_by_table.get(tr.table), S)

    if has_run_issues(report):
        ws = wb.create_sheet(S.get("sheets", "run_issues"))
        run_issues_sheet.build(ws, report, S)

    if any(tr.by_check for tr in report.table_reports):
        cs_ws = wb.create_sheet(S.get("sheets", "check_status"))
        check_status_sheet.build(cs_ws, report, S)

    if any(tr.metrics for tr in report.table_reports):
        m_ws = wb.create_sheet(S.get("sheets", "metrics"))
        metrics_sheet.build(m_ws, report, S)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
