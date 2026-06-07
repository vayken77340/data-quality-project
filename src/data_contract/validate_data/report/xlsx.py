"""XLSX report - business-facing workbook.

Sheets:
  Summary           - per-table totals + per-file breakdowns + traffic light
  <table>           - per-field violation counts + rate
  <table>_rejected  - one row per (source_row, violation), PK column-named headers

Single workbook overwritten at `epics/<epic>/validations/quality_report.xlsx`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from data_contract.contract import Contract
from data_contract.validate_data.runner import TableReport, ValidationReport
from data_contract.validate_data.violations import Violation


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_HEADER_ALIGN = Alignment(vertical="center", horizontal="left")

_FILL_RED = PatternFill("solid", fgColor="FCE4E4")
_FILL_YELLOW = PatternFill("solid", fgColor="FFF2CC")
_FILL_GREEN = PatternFill("solid", fgColor="E2EFDA")


def write_xlsx(report: ValidationReport, contracts_by_table: dict[str, Contract], out_path: Path) -> Path:
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    _populate_summary(summary, report)

    for tr in report.table_reports:
        contract = contracts_by_table.get(tr.table)
        per_table_ws = wb.create_sheet(_safe_name(tr.table))
        _populate_per_table(per_table_ws, tr)
        if tr.violations:
            rej_ws = wb.create_sheet(_safe_name(f"{tr.table}_rejected"))
            _populate_rejected(rej_ws, tr, contract)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Summary sheet
# ---------------------------------------------------------------------------


def _populate_summary(ws, report: ValidationReport) -> None:
    counts = report.summary_counts
    status = "FAIL" if report.has_errors else "PASS"
    ws.append(["Epic", report.epic])
    ws.append(["Generated at", report.generated_at])
    ws.append(["Status", status])
    ws.append(["Errors", counts["error"]])
    ws.append(["Warnings", counts["warning"]])
    ws.append(["Info", counts["info"]])
    bold = Font(bold=True)
    for r in range(1, ws.max_row + 1):
        ws.cell(row=r, column=1).font = bold
    # Status row color.
    status_cell = ws.cell(row=3, column=2)
    status_cell.fill = _FILL_RED if status == "FAIL" else _FILL_GREEN

    ws.append([])
    ws.append(["Table", "PK", "Files", "Total Rows", "Errors", "Warnings", "% Clean"])
    _style_header_row(ws, ncols=7)

    for tr in report.table_reports:
        n_err = sum(1 for v in tr.violations if v.severity == "error")
        n_warn = sum(1 for v in tr.violations if v.severity == "warning")
        rate = (tr.total_rows - len(tr.violations)) / tr.total_rows if tr.total_rows else 0.0
        ws.append([
            tr.table,
            ", ".join(tr.pk_fields) if tr.pk_fields else "-",
            len(tr.input_files),
            tr.total_rows,
            n_err,
            n_warn,
            f"{rate*100:.2f}%",
        ])
        if n_err > 0:
            for col in range(1, 8):
                ws.cell(row=ws.max_row, column=col).fill = _FILL_RED
        elif n_warn > 0:
            for col in range(1, 8):
                ws.cell(row=ws.max_row, column=col).fill = _FILL_YELLOW
        else:
            for col in range(1, 8):
                ws.cell(row=ws.max_row, column=col).fill = _FILL_GREEN

    # Per-file breakdown sections.
    for tr in report.table_reports:
        if not tr.input_files:
            continue
        ws.append([])
        ws.append([f"{tr.table} - per file"])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True, italic=True)
        ws.append(["Source File", "Rows", "Violations"])
        _style_header_row(ws, ncols=3)
        # Count violations per source_file.
        per_file_counts: Counter = Counter()
        for v in tr.violations:
            if v.source_file:
                per_file_counts[v.source_file] += 1
        for path, rows in tr.input_files:
            n = per_file_counts.get(path.name, 0)
            ws.append([path.name, rows, n])

    _autosize(ws, ncols=7, max_width=40)


# ---------------------------------------------------------------------------
# Per-table sheet
# ---------------------------------------------------------------------------


def _populate_per_table(ws, tr: TableReport) -> None:
    ws.append(["Field", "Violation Kind", "Severity", "Count", "Rate"])
    _style_header_row(ws, ncols=5)
    counter: Counter = Counter()
    for v in tr.violations:
        counter[(v.field or "-", v.kind, v.severity)] += 1
    for (field, kind, severity), n in sorted(counter.items()):
        rate = n / tr.total_rows if tr.total_rows else 0.0
        ws.append([field, kind, severity, n, f"{rate*100:.2f}%"])
    _autosize(ws, ncols=5, max_width=40)
    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(5)}{ws.max_row}"


# ---------------------------------------------------------------------------
# Rejected-rows sheet
# ---------------------------------------------------------------------------


def _populate_rejected(ws, tr: TableReport, contract: Contract | None) -> None:
    pk_field_names = list(tr.pk_fields)
    base_headers = ["Source File", "Source Row"]
    pk_headers = list(pk_field_names)
    tail_headers = ["Field", "Violation", "Severity", "Offending Value", "Expected"]
    headers = base_headers + pk_headers + tail_headers
    ws.append(headers)
    _style_header_row(ws, ncols=len(headers))

    # Sort: PK violations adjacent (sorted by PK value); others by (Source File, Source Row).
    pk_violations = [v for v in tr.violations if v.kind == "pk_not_unique"]
    other_violations = [v for v in tr.violations if v.kind != "pk_not_unique"]

    def _pk_sort_key(v: Violation):
        if not v.pk_values:
            return (tuple(),)
        return tuple(str(v.pk_values.get(c, "")) for c in pk_field_names)

    pk_violations.sort(key=_pk_sort_key)
    other_violations.sort(key=lambda v: (v.source_file or "", v.source_row or 0))
    ordered = pk_violations + other_violations

    for v in ordered:
        row: list[Any] = [v.source_file or "", v.source_row or ""]
        for pk_col in pk_field_names:
            if v.pk_values is None:
                row.append("")
            else:
                cell = v.pk_values.get(pk_col)
                row.append(cell if cell is not None else "<missing>")
        row.extend([
            v.field or "-",
            v.kind,
            v.severity,
            _stringify_cell(v.offending_value),
            v.expected,
        ])
        ws.append(row)
        if v.severity == "error":
            for col in range(1, len(headers) + 1):
                ws.cell(row=ws.max_row, column=col).fill = _FILL_RED
        elif v.severity == "warning":
            for col in range(1, len(headers) + 1):
                ws.cell(row=ws.max_row, column=col).fill = _FILL_YELLOW

    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    _autosize(ws, ncols=len(headers), max_width=50)


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------


def _style_header_row(ws, *, ncols: int) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=ws.max_row, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _autosize(ws, *, ncols: int, max_width: int = 50) -> None:
    for col_idx in range(1, ncols + 1):
        letter = get_column_letter(col_idx)
        widest = 0
        for row in range(1, ws.max_row + 1):
            v = ws.cell(row=row, column=col_idx).value
            if v is None:
                continue
            length = max((len(line) for line in str(v).splitlines()), default=0)
            if length > widest:
                widest = length
        ws.column_dimensions[letter].width = min(max(widest + 2, 10), max_width)


def _stringify_cell(value: Any) -> str:
    """Render a Violation's offending_value into a single XLSX cell.

    Scalar values pass through unchanged. Complex values (dict / list) are
    rendered as a compact key=value summary so the cell stays readable.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v!r}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(repr(x) for x in value)
    return repr(value)


_INVALID = set('[]:*?/\\')


def _safe_name(name: str) -> str:
    cleaned = "".join("_" if ch in _INVALID else ch for ch in name)
    return cleaned[:31] or "sheet"
