"""Per-epic XLSX data dictionary generator.

Renders one workbook per epic at `epics/<epic>/docs/data_dictionary.xlsx`:

  README sheet     - provenance (epic, version, source spec, counts).
  <table> sheets   - one per generated table; field rows with type, nullability,
                     PK/FK, description, and a human-readable constraint summary.
  Joins sheet      - present only when the epic has a joins contract.
  Drift sheet      - present only when contracts/drift/*.yaml exist; aggregates
                     every drift change across all version pairs.

Auto-filter + frozen header row + light column-width tuning so a business
consumer can sort/filter natively the moment they open it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook as WorkbookType

from dq_core._util import now_iso_z
from dq_core.yaml_io import load_yaml
from data_contract.generation.config import version_sort_key
from dq_core.contract import Contract, FieldContract
from data_contract.generation.joins import JoinsContract, JoinRow


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_HEADER_ALIGN = Alignment(vertical="center", horizontal="left")
_WRAP_ALIGN = Alignment(vertical="top", wrap_text=True)


TABLE_HEADERS = (
    "Field", "Type", "Nullable", "Max Length", "Precision", "Scale",
    "Primary Key", "Foreign Key", "Description", "Constraints",
)

JOINS_HEADERS = (
    "Source Table", "Source Column", "Target Table", "Target Column",
    "Type", "Cardinality", "Description", "Comment",
)

DRIFT_HEADERS = (
    "From Version", "To Version", "Table", "Field",
    "Kind", "Severity", "From", "To",
)


# ---------------------------------------------------------------------------
# Drift loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftEntry:
    from_version: str
    to_version: str
    table: str
    field: str | None
    kind: str
    severity: str
    from_value: Any
    to_value: Any


@dataclass(frozen=True)
class DriftAggregate:
    entries: list[DriftEntry]
    breaking: int
    additive: int
    cosmetic: int

    @property
    def total(self) -> int:
        return len(self.entries)


def load_drift_entries(contracts_dir: Path) -> DriftAggregate:
    """Walk `contracts/drift/*.yaml`, flatten each report's `changes:` list.

    Sorting: newest `to_version` first, then alphabetical table, then field.
    """
    drift_dir = contracts_dir / "drift"
    entries: list[DriftEntry] = []
    breaking = additive = cosmetic = 0
    if drift_dir.is_dir():
        for yaml_path in sorted(drift_dir.glob("*.yaml")):
            try:
                payload = load_yaml(yaml_path)
            except yaml.YAMLError:
                continue
            from_v = str(payload.get("from_version", ""))
            to_v = str(payload.get("to_version", ""))
            table = str(payload.get("table", ""))
            summary = payload.get("summary") or {}
            breaking += int(summary.get("breaking", 0))
            additive += int(summary.get("additive", 0))
            cosmetic += int(summary.get("cosmetic", 0))
            for change in payload.get("changes", []) or []:
                entries.append(DriftEntry(
                    from_version=from_v,
                    to_version=to_v,
                    table=table,
                    field=change.get("field"),
                    kind=str(change.get("kind", "")),
                    severity=str(change.get("severity", "")),
                    from_value=change.get("from"),
                    to_value=change.get("to"),
                ))

    entries.sort(
        key=lambda e: (
            tuple(-x if isinstance(x, int) else 0 for x in version_sort_key(e.to_version)),
            e.table,
            e.field or "",
            e.kind,
        )
    )
    return DriftAggregate(entries=entries, breaking=breaking, additive=additive, cosmetic=cosmetic)


def build_data_dictionary_workbook(
    *,
    epic: str,
    version: str,
    generated_at: str,
    spec_file: str,
    contracts: list[Contract],
    joins: JoinsContract | None,
    drift: DriftAggregate | None = None,
) -> WorkbookType:
    """Return an in-memory workbook with one sheet per table plus README/Joins/Drift.

    - `drift` drives the Drift sheet; when None or empty, the sheet is omitted.
    """
    wb = Workbook()
    # The default sheet is repurposed as the README.
    readme = wb.active
    readme.title = "README"
    _populate_readme(readme, epic=epic, version=version, generated_at=generated_at,
                     spec_file=spec_file, contracts=contracts, joins=joins)

    for contract in sorted(contracts, key=lambda c: c.table):
        sheet = wb.create_sheet(_safe_sheet_name(contract.table))
        _populate_table_sheet(sheet, contract)

    if joins is not None:
        joins_sheet = wb.create_sheet("Joins")
        _populate_joins_sheet(joins_sheet, joins.joins)

    if drift is not None and drift.total > 0:
        drift_sheet = wb.create_sheet("Drift")
        _populate_drift_sheet(drift_sheet, drift)

    return wb


def write_data_dictionary(
    *,
    epic_dir: Path,
    epic: str,
    version: str,
    spec_file: str,
    contracts: list[Contract],
    joins: JoinsContract | None,
    drift: DriftAggregate | None = None,
) -> Path:
    wb = build_data_dictionary_workbook(
        epic=epic, version=version, generated_at=now_iso_z(),
        spec_file=spec_file, contracts=contracts, joins=joins, drift=drift,
    )
    out = epic_dir / "docs" / "data_dictionary.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ---------------------------------------------------------------------------
# Sheet population
# ---------------------------------------------------------------------------


def _populate_readme(
    ws, *,
    epic: str, version: str, generated_at: str, spec_file: str,
    contracts: list[Contract], joins: JoinsContract | None,
) -> None:
    rows = [
        ("Epic",          epic),
        ("Version",       version),
        ("Generated at",  generated_at),
        ("Source spec",   spec_file),
        ("Tables",        len(contracts)),
    ]
    if joins is not None:
        rows.append(("Joins", len(joins.joins)))
    for label, value in rows:
        ws.append([label, value])
    bold = Font(bold=True)
    for row_idx in range(1, len(rows) + 1):
        ws.cell(row=row_idx, column=1).font = bold
    ws.append([])
    ws.append(["Note", "Generated from epics/<epic>/contracts/*.yaml. Do not edit by hand."])
    ws.cell(row=ws.max_row, column=1).font = bold
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 64


def _populate_drift_sheet(ws, drift: DriftAggregate) -> None:
    ws.append(list(DRIFT_HEADERS))
    _style_header(ws, len(DRIFT_HEADERS))
    for e in drift.entries:
        ws.append([
            e.from_version,
            e.to_version,
            e.table,
            e.field or "",
            e.kind,
            e.severity,
            _stringify(e.from_value),
            _stringify(e.to_value),
        ])
    _finalize_data_sheet(ws, ncols=len(DRIFT_HEADERS))


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return repr(v)


def _populate_table_sheet(ws, contract: Contract) -> None:
    ws.append(list(TABLE_HEADERS))
    _style_header(ws, len(TABLE_HEADERS))
    for f in contract.fields:
        ws.append(_field_row(f))
    _finalize_data_sheet(ws, ncols=len(TABLE_HEADERS))


def _populate_joins_sheet(ws, rows: list[JoinRow]) -> None:
    ws.append(list(JOINS_HEADERS))
    _style_header(ws, len(JOINS_HEADERS))
    for j in rows:
        ws.append([
            j.source_table,
            j.source_column,
            j.target_table,
            j.target_column,
            j.join_type,
            j.cardinality or "",
            j.description or "",
            j.comment or "",
        ])
    _finalize_data_sheet(ws, ncols=len(JOINS_HEADERS))


def _field_row(f: FieldContract) -> list[Any]:
    fk = ""
    if f.foreign_key:
        fk = f"{f.foreign_key.get('table', '')}.{f.foreign_key.get('column', '')}"
    return [
        f.name,
        f.type.value,
        _nullable_label(f.nullable),
        f.max_length if f.max_length is not None else "",
        f.precision if f.precision is not None else "",
        f.scale if f.scale is not None else "",
        "yes" if f.primary_key else "",
        fk,
        f.description or "",
        _format_constraints(f.constraints),
    ]


def _nullable_label(nullable: bool | None) -> str:
    if nullable is None:
        return ""
    return "yes" if nullable else "no"


def _format_constraints(constraints: dict[str, Any]) -> str:
    """Human-readable single-line summary for the Constraints cell.

    Recognized shapes:
      - allowed_values: [a, b, c]                    -> "allowed_values: [a, b, c]"
      - pattern: "^..."                              -> "pattern: ^..."
      - unique: true                                 -> "unique"
      - min_value: {value: 5, strict: false}         -> "min_value: 5 (>=)"
      - max_value: {value: 100, strict: true}        -> "max_value: 100 (<)"
      - anything else                                -> "<key>: <repr>"
    """
    parts: list[str] = []
    for key in sorted(constraints):
        v = constraints[key]
        if key == "min_value" and isinstance(v, dict):
            op = ">" if v.get("strict") else ">="
            parts.append(f"min_value: {v.get('value')} ({op})")
        elif key == "max_value" and isinstance(v, dict):
            op = "<" if v.get("strict") else "<="
            parts.append(f"max_value: {v.get('value')} ({op})")
        elif key == "allowed_values" and isinstance(v, list):
            parts.append(f"allowed_values: [{', '.join(str(x) for x in v)}]")
        elif key == "unique" and v is True:
            parts.append("unique")
        elif key == "pattern" and isinstance(v, str):
            parts.append(f"pattern: {v}")
        else:
            parts.append(f"{key}: {v}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------


def _style_header(ws, ncols: int) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _finalize_data_sheet(ws, *, ncols: int) -> None:
    if ws.max_row <= 1:
        # Header-only sheet — still freeze the header and apply auto-filter so
        # the experience is consistent for empty tables.
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}1"
        return
    last_col_letter = get_column_letter(ncols)
    last_row = ws.max_row
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"

    # Set the Description and Constraints cells (last two columns of table
    # sheets, last two of joins sheets) to wrap.
    for row in range(2, last_row + 1):
        for col in (ncols - 1, ncols):
            ws.cell(row=row, column=col).alignment = _WRAP_ALIGN

    _autosize(ws, ncols=ncols)


def _autosize(ws, *, ncols: int, max_width: int = 60) -> None:
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


_INVALID_SHEET_CHARS = set('[]:*?/\\')


def _safe_sheet_name(name: str) -> str:
    """openpyxl rejects sheet names containing `[]:*?/\\` and longer than 31 chars."""
    cleaned = "".join("_" if ch in _INVALID_SHEET_CHARS else ch for ch in name)
    return cleaned[:31] or "sheet"
