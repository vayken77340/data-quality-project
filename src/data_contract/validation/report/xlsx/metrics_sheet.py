"""Metrics sheet: per-table sections with table-scope band + field-scope grid."""

from __future__ import annotations

from typing import Any

from openpyxl.styles import Font

from data_contract.validation.models import ValidationReport
from data_contract.validation.report.xlsx._common import (
    append, autosize, style_header_row,
)
from data_contract.validation.report.xlsx._styles import (
    HEADER_ALIGN, HEADER_FILL,
)


def build(ws, report: ValidationReport, S) -> None:
    """Render per-table metrics sections: header band for table-scope metrics,
    then a field x metric grid for field-scope metrics."""
    M = S.get("metrics_sheet", "labels")
    field_header_label = M.get("field", "Field")
    table_scope_label = M.get("table_scope", "Table-scope")
    metric_labels = S.get("metrics_sheet", "metric_labels")
    section_font = Font(bold=True, color="FFFFFF", size=12)

    first_section = True
    for tr in report.table_reports:
        if not tr.metrics:
            continue
        if not first_section:
            append(ws, [])
        first_section = False

        append(ws, [S.fmt("metrics_sheet", "table_heading_template", table=tr.table)])
        heading_row = ws.max_row
        heading_cell = ws.cell(row=heading_row, column=1)
        heading_cell.font = section_font
        heading_cell.fill = HEADER_FILL
        heading_cell.alignment = HEADER_ALIGN

        table_metrics = [(n, r) for n, r in tr.metrics.items() if r.scope == "table"]
        if table_metrics:
            append(ws, [table_scope_label])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True, italic=True)
            for name, result in sorted(table_metrics):
                append(ws, [
                    metric_labels.get(name, name),
                    result.values.get("__table__"),
                ])

        field_metrics = [(n, r) for n, r in tr.metrics.items() if r.scope == "field"]
        if field_metrics:
            if table_metrics:
                append(ws, [])
            sorted_field_metrics = sorted(field_metrics)
            headers = [field_header_label] + [
                metric_labels.get(n, n) for n, _ in sorted_field_metrics
            ]
            append(ws, headers)
            style_header_row(ws, ncols=len(headers))

            # Field order: union of fields across metric results, preserving
            # contract order when possible. Falls back to alphabetical.
            field_names: list[str] = []
            seen: set[str] = set()
            for _, result in sorted_field_metrics:
                for fname in result.values:
                    if fname == "__table__" or fname in seen:
                        continue
                    seen.add(fname)
                    field_names.append(fname)
            field_names.sort()
            for fname in field_names:
                row: list[Any] = [fname]
                for _, result in sorted_field_metrics:
                    val = result.values.get(fname, "")
                    if isinstance(val, float):
                        row.append(f"{val:.2f}")
                    else:
                        row.append(val)
                append(ws, row)

    autosize(ws, ncols=20, max_width=30)
