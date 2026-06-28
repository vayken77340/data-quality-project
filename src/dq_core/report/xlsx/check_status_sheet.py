"""Check Status sheet: tables x checks status grid with colour tints."""

from __future__ import annotations

from typing import Any

from dq_core.report_models import ValidationReport
from dq_core.report.xlsx._common import (
    append, autosize, style_header_row,
)
from dq_core.report.xlsx._styles import CHECK_STATUS_FILL


def build(ws, report: ValidationReport, S) -> None:
    """Render the tables x checks status grid.

    Rows are tables, columns are checks, cells are the coloured status plus
    inline counts. Column order: every check present in any table_report's
    `by_check`, sorted alphabetically.
    """
    CS = S.get("check_status_sheet", "labels")
    SL = S.get("check_status_sheet", "status_labels")
    cell_tpl = S.get("check_status_sheet", "cell_template_row")
    cell_tpl_table = S.get("check_status_sheet", "cell_template_table")
    skipped_label = SL.get("SKIPPED", "--")
    na_label = SL.get("NA", "n/a")

    check_names: set[str] = set()
    for tr in report.table_reports:
        check_names.update(tr.by_check.keys())
    sorted_checks = sorted(check_names)

    header = [CS.get("table", "Table")] + sorted_checks
    append(ws, header)
    style_header_row(ws, ncols=len(header))

    for tr in report.table_reports:
        row: list[Any] = [tr.table]
        for check in sorted_checks:
            status = tr.by_check.get(check)
            if status is None:
                row.append("")
                continue
            label = SL.get(status.status, status.status)
            if status.status == "SKIPPED":
                row.append(skipped_label)
            elif status.status == "NA":
                row.append(na_label)
            elif status.scope == "table":
                row.append(cell_tpl_table.format(
                    label=label,
                    count=status.violation_count,
                ))
            else:
                row.append(cell_tpl.format(
                    label=label,
                    pass_rows=status.pass_rows if status.pass_rows is not None else 0,
                    warning_rows=status.warning_rows,
                    error_rows=status.error_rows,
                ))
        append(ws, row)
        for col_idx, check in enumerate(sorted_checks, start=2):
            status = tr.by_check.get(check)
            if status is None:
                continue
            fill = CHECK_STATUS_FILL.get(status.status)
            if fill is not None:
                ws.cell(row=ws.max_row, column=col_idx).fill = fill

    if ws.max_row > 1:
        ws.freeze_panes = "B2"
    autosize(ws, ncols=len(header), max_width=40)
