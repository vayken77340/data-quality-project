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

The rejected sheet honours `config.settings.rejected_row_cap` and appends a
truncation marker row when over cap.

Every user-facing string in this module is sourced from
`configs/report_strings.yaml` via `report.strings.load_strings()`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# XML 1.0 forbids most C0 control characters in element / attribute content,
# and Excel will refuse to open a workbook that contains any. openpyxl does
# not strip them, so a stray 0x00 / 0x07 / 0x1B in source data (we've seen
# them coming out of Excel files re-exported by legacy tools) makes
# `wb.save()` produce a file that throws "file format or extension is not
# valid" on open. We sanitize every cell value before appending to the sheet.
# Tab (0x09), LF (0x0a), and CR (0x0d) are the ONLY C0 controls Excel allows
# and are preserved.
_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _safe_cell(value: Any) -> Any:
    """Strip Excel-illegal control characters from string cell values."""
    if isinstance(value, str):
        return _ILLEGAL_XLSX_CHARS.sub("", value)
    return value


def _append(ws, row: list[Any]) -> None:
    """`ws.append` wrapper that sanitizes every cell first."""
    ws.append([_safe_cell(v) for v in row])

from data_contract.contract import Contract
from data_contract.validation.report.colors import hex_
from data_contract.validation.report.dimensions import (
    compute_overall_score,
    violation_kind_for_check,
)
from data_contract.validation.report.hints import HINTS
from data_contract.validation.report.strings import load_strings
from data_contract.validation.runner import (
    RejectedRow,
    TableReport,
    ValidationReport,
)


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor=hex_("header"))
_HEADER_ALIGN = Alignment(vertical="center", horizontal="left")

_FILL_RED = PatternFill("solid", fgColor=hex_("red"))
_FILL_ORANGE = PatternFill("solid", fgColor=hex_("orange"))
_FILL_GREEN = PatternFill("solid", fgColor=hex_("green"))


def write_xlsx(
    report: ValidationReport,
    contracts_by_table: dict[str, Contract],
    out_path: Path,
) -> Path:
    S = load_strings()
    wb = Workbook()
    # Initial active sheet becomes the Run sheet -- rename it first.
    run_ws = wb.active
    run_ws.title = S.get("sheets", "run")
    _populate_run(run_ws, report, S)

    summary_ws = wb.create_sheet(S.get("sheets", "summary"))
    _populate_summary(summary_ws, report, S)

    if any(tr.profile is not None for tr in report.table_reports):
        profile_ws = wb.create_sheet(S.get("sheets", "profile"))
        _populate_profile_all(profile_ws, report, S)

    checks_ws = wb.create_sheet(S.get("sheets", "checks"))
    _populate_checks(checks_ws, report, S)

    for tr in report.table_reports:
        if tr.rejected_rows:
            sheet_name = _safe_name(S.fmt("sheets", "rejected_pattern", table=tr.table))
            ws = wb.create_sheet(sheet_name)
            _populate_rejected(ws, tr, contracts_by_table.get(tr.table), S)

    if _has_run_issues(report):
        ws = wb.create_sheet(S.get("sheets", "run_issues"))
        _populate_run_issues(ws, report, S)

    if any(tr.by_check for tr in report.table_reports):
        cs_ws = wb.create_sheet(S.get("sheets", "check_status"))
        _populate_check_status(cs_ws, report, S)

    if any(tr.metrics for tr in report.table_reports):
        m_ws = wb.create_sheet(S.get("sheets", "metrics"))
        _populate_metrics_all(m_ws, report, S)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Run sheet
# ---------------------------------------------------------------------------


def _populate_run(ws, report: ValidationReport, S) -> None:
    rm = report.run_metadata
    L = S.get("run_sheet", "labels")
    no_target = S.get("run_sheet", "no_target")
    pass_label = S.get("status", "pass")
    fail_label = S.get("status", "fail")
    rows: list[tuple[str, Any]] = [
        (L["epic"], report.epic),
        (L["generated_at"], report.generated_at),
        (L["status"], rm.status if rm else (fail_label if report.has_errors else pass_label)),
        (L["status_reason"], rm.status_reason if rm else ""),
        (L["duration_ms"], rm.duration_ms if rm else 0),
        (L["target"], (rm.target["name"] if rm and rm.target else no_target)),
        (L["checks_enabled"], ", ".join(rm.checks_enabled) if rm else ""),
        (L["checks_disabled"], ", ".join(rm.checks_disabled) if rm else ""),
        (L["cli_args"], " ".join(rm.cli_args) if rm else ""),
    ]
    bold = Font(bold=True)
    for label, value in rows:
        _append(ws, [label, value])
        ws.cell(row=ws.max_row, column=1).font = bold
    status_row = 3
    status_cell = ws.cell(row=status_row, column=2)
    status_cell.fill = (
        _FILL_RED if status_cell.value == fail_label
        else _FILL_GREEN if status_cell.value == pass_label
        else _FILL_ORANGE
    )

    _append(ws, [])
    _append(ws, [S.get("run_sheet", "contract_versions_heading")])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, italic=True)
    if rm:
        for tbl, ver in rm.contracts.items():
            _append(ws, [tbl, ver])
    _autosize(ws, ncols=2, max_width=80)


# ---------------------------------------------------------------------------
# Summary sheet (Summary + Dimensions merged)
# ---------------------------------------------------------------------------


_COMPLETENESS_KINDS = frozenset({"nullable_violation", "column_missing"})
_PK_UNIQUENESS_KINDS = frozenset({"pk_not_unique"})
_FK_CONSISTENCY_KINDS = frozenset({"fk_not_found"})


def _filtered_score(violations, kinds: frozenset, total_rows: int) -> float:
    """Score 0..100 considering only error-severity violations in `kinds`.

    Same row-based formula as the overall score: distinct (source_file,
    source_row) tuples with at least one matching error mark the row dirty.
    Empty tables score 0.0 (no rows to be clean).
    """
    if total_rows <= 0:
        return 0.0
    dirty: set[tuple] = set()
    for v in violations:
        if v.severity != "error" or v.kind not in kinds:
            continue
        if v.source_file is not None and v.source_row is not None:
            dirty.add((v.source_file, v.source_row))
    clean = max(total_rows - len(dirty), 0)
    return round(clean / total_rows * 100, 2)


def _has_fk_check(report: ValidationReport) -> bool:
    rm = report.run_metadata
    return bool(rm and "fk_existence" in rm.checks_enabled)


def _populate_summary(ws, report: ValidationReport, S) -> None:
    counts = report.summary_counts
    overall = compute_overall_score(
        [(tr.total_rows, tr.score) for tr in report.table_reports if tr.score]
    )
    pass_label = S.get("status", "pass")
    fail_label = S.get("status", "fail")
    status = fail_label if report.has_errors else pass_label
    bold = Font(bold=True)

    OL = S.get("summary_sheet", "overall_labels")
    _append(ws, [OL["score"], f"{overall.score:.1f}"])
    _append(ws, [OL["status"], status])
    _append(ws, [OL["errors"], counts["error"]])
    _append(ws, [OL["warnings"], counts["warning"]])
    _append(ws, [OL["info"], counts["info"]])
    for r in range(1, ws.max_row + 1):
        ws.cell(row=r, column=1).font = bold
    status_cell = ws.cell(row=2, column=2)
    status_cell.fill = _FILL_RED if status == fail_label else _FILL_GREEN

    _append(ws, [])
    base_cols = list(S.get("summary_sheet", "scorecard_headers"))
    DL = S.get("summary_sheet", "dimension_labels")
    show_fk = _has_fk_check(report)
    dim_columns: list[tuple[str, frozenset]] = [
        (DL["completeness"], _COMPLETENESS_KINDS),
        (DL["pk_uniqueness"], _PK_UNIQUENESS_KINDS),
    ]
    if show_fk:
        dim_columns.append((DL["fk_consistency"], _FK_CONSISTENCY_KINDS))
    headers = base_cols + [name for name, _ in dim_columns]
    _append(ws, headers)
    _style_header_row(ws, ncols=len(headers))

    no_pk = S.get("summary_sheet", "no_pk_placeholder")

    # Track per-column running totals for the bottom Totals row.
    sum_files = 0
    sum_total_rows = 0
    sum_clean = 0
    sum_err = 0
    sum_warn = 0
    sum_info = 0
    # For weighted per-dimension and overall pass-rate totals.
    dim_weighted: dict[str, float] = {name: 0.0 for name, _ in dim_columns}
    overall_weighted_score = 0.0
    total_weight = 0

    for tr in report.table_reports:
        n_err = sum(1 for v in tr.violations if v.severity == "error")
        n_warn = sum(1 for v in tr.violations if v.severity == "warning")
        n_info = sum(1 for v in tr.violations if v.severity == "info")
        pass_rate = tr.score.score if tr.score else 100.0
        clean = tr.score.clean_rows if tr.score else tr.total_rows
        row: list[Any] = [
            tr.table,
            ", ".join(tr.pk_fields) if tr.pk_fields else no_pk,
            len(tr.input_files),
            clean,
            tr.total_rows,
            n_err,
            n_warn,
            n_info,
            f"{pass_rate:.1f}",
        ]
        for _, kinds in dim_columns:
            row.append(f"{_filtered_score(tr.violations, kinds, tr.total_rows):.1f}")
        _append(ws, row)
        fill = _FILL_RED if n_err else (_FILL_ORANGE if n_warn else _FILL_GREEN)
        for c in range(1, len(headers) + 1):
            ws.cell(row=ws.max_row, column=c).fill = fill

        # Accumulate totals.
        sum_files += len(tr.input_files)
        sum_total_rows += tr.total_rows
        sum_clean += clean
        sum_err += n_err
        sum_warn += n_warn
        sum_info += n_info
        if tr.total_rows > 0:
            overall_weighted_score += pass_rate * tr.total_rows
            for name, kinds in dim_columns:
                dim_weighted[name] += _filtered_score(
                    tr.violations, kinds, tr.total_rows
                ) * tr.total_rows
            total_weight += tr.total_rows

    # Totals row.
    overall_pass = overall_weighted_score / total_weight if total_weight else 0.0
    totals_label = S.get("summary_sheet", "totals_label")
    totals_row: list[Any] = [
        totals_label, "", sum_files, sum_clean, sum_total_rows,
        sum_err, sum_warn, sum_info, f"{overall_pass:.1f}",
    ]
    for name, _ in dim_columns:
        avg = dim_weighted[name] / total_weight if total_weight else 0.0
        totals_row.append(f"{avg:.1f}")
    _append(ws, totals_row)
    # Style the totals row: bold + neutral header fill to distinguish from data rows.
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=ws.max_row, column=c)
        cell.font = Font(bold=True)

    _autosize(ws, ncols=len(headers), max_width=40)


# ---------------------------------------------------------------------------
# Profile sheet (all tables stacked, one section per table)
# ---------------------------------------------------------------------------


def _populate_profile_all(ws, report: ValidationReport, S) -> None:
    headers = list(S.get("profile_sheet", "headers"))
    pk_indicator = S.get("profile_sheet", "pk_indicator")
    fk_indicator = S.get("profile_sheet", "fk_indicator")
    section_font = Font(bold=True, color="FFFFFF", size=12)
    first_section = True
    for tr in report.table_reports:
        if tr.profile is None:
            continue
        if not first_section:
            _append(ws, [])
        first_section = False

        _append(ws, [S.fmt("profile_sheet", "table_heading_template", table=tr.table)])
        heading_row = ws.max_row
        ws.merge_cells(start_row=heading_row, start_column=1,
                       end_row=heading_row, end_column=len(headers))
        heading_cell = ws.cell(row=heading_row, column=1)
        heading_cell.font = section_font
        heading_cell.fill = _HEADER_FILL
        heading_cell.alignment = _HEADER_ALIGN

        _append(ws, headers)
        _style_header_row(ws, ncols=len(headers))

        # Null #, Null %, Distinct come from the metrics registry now -- one
        # source of truth. Disabled metrics surface as "--" in their column.
        nc = tr.metrics["null_count"].values if "null_count" in tr.metrics else {}
        np_ = tr.metrics["null_percentage"].values if "null_percentage" in tr.metrics else {}
        dc = tr.metrics["distinct_count"].values if "distinct_count" in tr.metrics else {}
        for f in tr.profile.fields:
            null_count = nc.get(f.name)
            null_pct = np_.get(f.name)
            distinct_count = dc.get(f.name)
            _append(ws, [
                f.name,
                f.type,
                f.type_format,
                pk_indicator if f.is_pk else "",
                fk_indicator if f.is_fk else "",
                null_count if null_count is not None else "--",
                f"{null_pct:.1f}" if null_pct is not None else "--",
                distinct_count if distinct_count is not None else "--",
                f.total,
            ])
    _autosize(ws, ncols=len(headers), max_width=40)


# ---------------------------------------------------------------------------
# Checks sheet
# ---------------------------------------------------------------------------


def _populate_checks(ws, report: ValidationReport, S) -> None:
    """List every enabled check with its YAML-supplied description and the
    violation kind it emits. Disabled checks are intentionally omitted."""
    headers = list(S.get("checks_sheet", "headers"))
    _append(ws, headers)
    _style_header_row(ws, ncols=len(headers))
    rm = report.run_metadata
    if rm is None:
        return
    for name in rm.checks_enabled:
        try:
            vk = violation_kind_for_check(name)
        except KeyError:
            vk = ""
        _append(ws, [name, rm.checks_descriptions.get(name, ""), vk])
    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    _autosize(ws, ncols=len(headers), max_width=100)


# ---------------------------------------------------------------------------
# Rejected sheet (one per table; wide format with per-cell severity colouring)
# ---------------------------------------------------------------------------


def _populate_rejected(
    ws,
    tr: TableReport,
    contract: Contract | None,
    S,
) -> None:
    """One row per source row.

    Layout: `Severity | Source File | <PK fields | OR Source Row> |
              <only the fields that actually have a violation in this table> |
              Check | Expected`.

    When a row violates multiple checks, the Check and Expected cells stack
    one line per violation (newline-joined) so a reader can map check N to
    expected N at the same vertical offset. Field-block cells get the
    offending value for that field only -- if the same field is hit by
    multiple checks on this row, the values stack the same way.

    Only contract fields that have at least one violation across the table's
    rejected rows are emitted as columns. Only the Severity cell is
    colour-tinted; the rest of the row stays uncoloured so the eye lands
    on the severity column.
    """
    H = S.get("rejected_sheet", "headers")

    # Restrict the contract-field block to fields that actually have a
    # violation in this table. Preserve the contract's declared field order
    # so columns stay stable across runs.
    violating_fields_set = {
        v.get("field") for r in tr.rejected_rows for v in r.violations if v.get("field")
    }
    all_contract_fields = list(contract.fields) if contract else []
    contract_fields = [f.name for f in all_contract_fields if f.name in violating_fields_set]

    # Logical-type lookup per field so the type_coercion label can carry
    # the user-facing type name ("Type violation: integer", etc.).
    field_type_label: dict[str, str] = {}
    if contract is not None:
        type_user_labels = S.get("rejected_sheet", "type_user_labels")
        for f in all_contract_fields:
            field_type_label[f.name] = type_user_labels.get(
                f.type.value, f.type.value
            )

    pk_fields = list(tr.pk_fields)
    has_pk = bool(pk_fields)
    key_headers = list(pk_fields) if has_pk else [H["source_row"]]

    # Severity is the first column.
    headers: list[str] = (
        [H["severity"], H["source_file"], *key_headers]
        + list(contract_fields)
        + [H["check"], H["expected"]]
    )
    _append(ws, headers)
    _style_header_row(ws, ncols=len(headers))

    severity_col = 1
    # 1-based: severity (1) + source_file (1) + key columns + 1 to step past them.
    field_block_start = 1 + 1 + len(key_headers) + 1
    field_to_col = {
        name: field_block_start + idx for idx, name in enumerate(contract_fields)
    }
    n_field_cols = len(contract_fields)
    check_col = field_block_start + n_field_cols
    expected_col = check_col + 1

    wrap_align = Alignment(wrap_text=True, vertical="top")

    for r in tr.rejected_rows:
        # Stable ordering inside the row: errors before warnings before info,
        # then by field name. Check and Expected are stacked in this order so
        # readers can map line N of one cell to line N of the other.
        ordered = sorted(
            r.violations,
            key=lambda v: (_severity_rank(v.get("severity")), v.get("field") or ""),
        )

        # Worst severity across the row drives the cell tint.
        worst_severity = r.worst_severity or (
            ordered[0].get("severity") if ordered else ""
        )

        row: list[Any] = [(worst_severity or "").upper(), r.source_file]
        if has_pk:
            for pk_name in pk_fields:
                row.append(_stringify_cell(r.pk_values.get(pk_name)))
        else:
            row.append(r.source_row)

        # Build per-field stacks of offending values (one field may be hit
        # by multiple checks on the same row).
        field_block_values: list[list[str]] = [[] for _ in contract_fields]
        check_lines: list[str] = []
        expected_lines: list[str] = []
        for v in ordered:
            check_lines.append(_check_label(v, S, field_type_label))
            expected_lines.append(v.get("expected", ""))
            field = v.get("field")
            if field and field in field_to_col:
                col_idx = field_to_col[field] - field_block_start
                raw = v.get("offending_value")
                rendered = "(null)" if raw is None else _stringify_cell(raw)
                field_block_values[col_idx].append(rendered)

        row.extend("\n".join(values) for values in field_block_values)
        row.append("\n".join(check_lines))
        row.append("\n".join(expected_lines))
        _append(ws, row)

        # Severity tint on column 1.
        fill = _severity_fill(worst_severity)
        if fill is not None:
            ws.cell(row=ws.max_row, column=severity_col).fill = fill

        # Wrap stacked cells so all lines stay visible.
        excel_row = ws.max_row
        ws.cell(row=excel_row, column=check_col).alignment = wrap_align
        ws.cell(row=excel_row, column=expected_col).alignment = wrap_align
        for idx, values in enumerate(field_block_values):
            if len(values) > 1:
                ws.cell(
                    row=excel_row, column=field_block_start + idx
                ).alignment = wrap_align

    if tr.rejected_rows_truncated:
        _append(ws, [
            S.fmt("rejected_sheet", "truncated_template",
                  count=tr.rejected_rows_truncated)
        ])
        ws.cell(row=ws.max_row, column=1).font = Font(italic=True)

    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    _autosize(ws, ncols=len(headers), max_width=80)


def _severity_rank(severity: str | None) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(severity or "", 3)


def _check_label(v: dict, S, field_type_label: dict[str, str]) -> str:
    """Map a violation kind to its business-friendly label.

    The `type_coercion_violation` template expects a `{type}` placeholder,
    which we resolve from the contract field's declared type. Falls back
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


def _severity_fill(severity: str | None) -> PatternFill | None:
    if severity == "error":
        return _FILL_RED
    if severity == "warning":
        return _FILL_ORANGE
    return None


# ---------------------------------------------------------------------------
# Run issues sheet
# ---------------------------------------------------------------------------


def _table_level_violations(report: ValidationReport):
    """Yield every violation that isn't tied to a specific source row.

    This is the catch-all for schema gaps (column_missing, extra_column)
    and operational failures (no_input_files, parser_failure,
    fk_target_table_not_loaded). Row-keyed violations are handled by the
    per-table `<table>_rejected` sheet.
    """
    for tr in report.table_reports:
        for v in tr.violations:
            if v.source_row is None:
                yield v


def _has_run_issues(report: ValidationReport) -> bool:
    return any(True for _ in _table_level_violations(report))


def _populate_run_issues(ws, report: ValidationReport, S) -> None:
    """Populate the "Table issues" sheet (`run_issues_sheet` in YAML for
    backward compatibility). Headers are Severity / Table / Check / Field /
    Expected / Hint; severity cell is colour-tinted to mirror the rejected
    sheet."""
    headers = list(S.get("run_issues_sheet", "headers"))
    field_placeholder = S.get("run_issues_sheet", "field_placeholder")
    labels = S.get("rejected_sheet", "check_labels")
    _append(ws, headers)
    _style_header_row(ws, ncols=len(headers))

    rows = sorted(
        _table_level_violations(report),
        key=lambda v: (
            _severity_rank(v.severity),
            v.table or "",
            v.kind,
            v.field or "",
        ),
    )

    for v in rows:
        try:
            hint = HINTS.get(v.kind, "")
        except Exception:
            hint = ""
        # Friendly label (e.g. "Column missing in source") for the Check cell.
        # No {type} placeholders apply to table-level kinds, so a plain lookup
        # is enough.
        check = labels.get(v.kind, v.kind)
        _append(ws, [
            (v.severity or "").upper(),
            v.table,
            check,
            v.field or field_placeholder,
            v.expected,
            hint,
        ])
        fill = _severity_fill(v.severity)
        if fill is not None:
            ws.cell(row=ws.max_row, column=1).fill = fill

    if ws.max_row > 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    _autosize(ws, ncols=len(headers), max_width=80)


# ---------------------------------------------------------------------------
# Check status sheet (tables x checks grid)
# ---------------------------------------------------------------------------


_CHECK_STATUS_FILL = {
    "OK": _FILL_GREEN,
    "WARNING": _FILL_ORANGE,
    "ERROR": _FILL_RED,
}


def _populate_check_status(ws, report: ValidationReport, S) -> None:
    """Render the tables x checks status grid.

    Rows are tables, columns are checks, cells are the colored status
    plus inline counts. Column order: every check present in any
    table_report's `by_check`, sorted alphabetically.
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
    _append(ws, header)
    _style_header_row(ws, ncols=len(header))

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
        _append(ws, row)
        # Colour-tint each check cell by status.
        for col_idx, check in enumerate(sorted_checks, start=2):
            status = tr.by_check.get(check)
            if status is None:
                continue
            fill = _CHECK_STATUS_FILL.get(status.status)
            if fill is not None:
                ws.cell(row=ws.max_row, column=col_idx).fill = fill

    if ws.max_row > 1:
        ws.freeze_panes = "B2"
    _autosize(ws, ncols=len(header), max_width=40)


# ---------------------------------------------------------------------------
# Metrics sheet (per-table sections)
# ---------------------------------------------------------------------------


def _populate_metrics_all(ws, report: ValidationReport, S) -> None:
    """Render per-table metrics sections: header band for table-scope
    metrics, then a field x metric grid for field-scope metrics.
    """
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
            _append(ws, [])
        first_section = False

        _append(ws, [S.fmt("metrics_sheet", "table_heading_template", table=tr.table)])
        heading_row = ws.max_row
        heading_cell = ws.cell(row=heading_row, column=1)
        heading_cell.font = section_font
        heading_cell.fill = _HEADER_FILL
        heading_cell.alignment = _HEADER_ALIGN

        # Table-scope metrics: small key/value band.
        table_metrics = [(n, r) for n, r in tr.metrics.items() if r.scope == "table"]
        if table_metrics:
            _append(ws, [table_scope_label])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True, italic=True)
            for name, result in sorted(table_metrics):
                _append(ws, [
                    metric_labels.get(name, name),
                    result.values.get("__table__"),
                ])

        # Field-scope grid.
        field_metrics = [(n, r) for n, r in tr.metrics.items() if r.scope == "field"]
        if field_metrics:
            if table_metrics:
                _append(ws, [])
            sorted_field_metrics = sorted(field_metrics)
            headers = [field_header_label] + [
                metric_labels.get(n, n) for n, _ in sorted_field_metrics
            ]
            _append(ws, headers)
            _style_header_row(ws, ncols=len(headers))

            # Field order: union of fields across metric results, preserving
            # contract order when possible. Fall back to alphabetical.
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
                _append(ws, row)

    _autosize(ws, ncols=20, max_width=30)


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
