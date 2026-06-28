"""Shared row-emission, column-sizing, and value-rendering utilities.

Every XLSX sheet builder writes via `append`, freezes the header via
`style_header_row`, and auto-sizes via `autosize`. Centralising these keeps
the sheet modules focused on their own data shape.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from openpyxl.utils import get_column_letter

from dq_core.report_models import ValidationReport
from dq_core.report.xlsx._styles import (
    HEADER_ALIGN, HEADER_FILL, HEADER_FONT,
)


# XML 1.0 forbids most C0 control characters in element / attribute content,
# and Excel will refuse to open a workbook that contains any. openpyxl does
# not strip them, so a stray 0x00 / 0x07 / 0x1B in source data (we've seen
# them coming out of Excel files re-exported by legacy tools) makes
# `wb.save()` produce a file that throws "file format or extension is not
# valid" on open. Sanitise every cell value before appending.
# Tab (0x09), LF (0x0a), and CR (0x0d) are the ONLY C0 controls Excel allows
# and are preserved.
_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_INVALID_SHEET_CHARS = set('[]:*?/\\')


def safe_cell(value: Any) -> Any:
    """Strip Excel-illegal control characters from string cell values."""
    if isinstance(value, str):
        return _ILLEGAL_XLSX_CHARS.sub("", value)
    return value


def append(ws, row: list[Any]) -> None:
    """`ws.append` wrapper that sanitises every cell first."""
    ws.append([safe_cell(v) for v in row])


def style_header_row(ws, *, ncols: int) -> None:
    """Apply the header font / fill / alignment to the most recently appended row."""
    for col in range(1, ncols + 1):
        cell = ws.cell(row=ws.max_row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN


def autosize(ws, *, ncols: int, max_width: int = 50) -> None:
    """Set each column's width to the longest single-line value (capped at
    `max_width`). Multi-line cells widen to the widest individual line."""
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


def stringify_cell(value: Any) -> str:
    """Render `value` as the canonical string an XLSX cell would carry.

    None -> empty string (Excel renders blank). Dict / list / tuple use repr
    so structural detail is visible in the rejected sheet rather than the
    bare `repr(<dict>)` output that Excel can't render.
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


def safe_sheet_name(name: str) -> str:
    """Excel sheet names: forbid `[ ] : * ? / \\` and cap at 31 chars."""
    cleaned = "".join("_" if ch in _INVALID_SHEET_CHARS else ch for ch in name)
    return cleaned[:31] or "sheet"


def severity_rank(severity: str | None) -> int:
    """Sort key for violations within a row: errors first, then warnings, info."""
    return {"error": 0, "warning": 1, "info": 2}.get(severity or "", 3)


def check_label(v: dict, S, field_type_label: dict[str, str]) -> str:
    """Map a violation kind to its business-friendly label.

    The `type_coercion_violation` template expects a `{type}` placeholder,
    which is resolved from the contract field's declared type. Falls back
    to the raw kind string for unknown kinds so nothing silently disappears.
    """
    kind = v.get("kind") or ""
    labels = S.get("rejected_sheet", "check_labels")
    template = labels.get(kind)
    if template is None:
        return kind
    if "{type}" in template:
        field = v.get("field") or ""
        type_label = field_type_label.get(field, "")
        return template.format(type=type_label)
    return template


def table_level_violations(report: ValidationReport) -> Iterator:
    """Yield every violation that isn't tied to a specific source row.

    Catch-all for schema gaps (column_missing, extra_column) and operational
    failures (no_input_files, parser_failure, fk_target_table_not_loaded).
    Row-keyed violations are handled by the per-table rejected sheet.
    """
    for tr in report.table_reports:
        for v in tr.violations:
            if v.source_row is None:
                yield v


def has_run_issues(report: ValidationReport) -> bool:
    return any(True for _ in table_level_violations(report))
