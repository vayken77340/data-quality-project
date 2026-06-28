"""Spec workbook reader: open, locate headers, iterate raw field rows.

The thin layer between openpyxl and the build pipeline. Locates each
table's spec sheet, finds the header row (header may not be row 1 --
spec authors sometimes leave a title block above), maps configured
column names to column indexes via `header_matcher`, and streams one
`RawField` per data row.

Everything returned is RAW -- no type parsing, no nullable normalisation,
no slugify. Those are `generation/builder.py`'s concern. This module is
the only generation-side module that touches openpyxl directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from data_contract.generation.config import ColumnMapping
from data_contract.errors import RejectionError, SpecReaderError
from data_contract.generation.header_matcher import find_column, normalize
from data_contract.generation.sheet_io import (
    HEADER_SEARCH_DEPTH,
    cell as _cell,
    locate_header_row,
    row_is_empty as _row_is_empty,
)


@dataclass(frozen=True)
class RawField:
    sheet_row: int
    name_raw: object | None
    type_raw: object | None
    description_raw: object | None
    nullable_raw: object | None
    table_raw: object | None  # None when the sheet has no Table column
    db_name_raw: object | None = None  # None when the sheet has no `Nom BDD` column
    extras: dict[str, object | None] = field(default_factory=dict)  # constraint name -> raw cell


@dataclass(frozen=True)
class SheetSpec:
    sheet_name: str
    header_row: int
    col_idx: dict[str, int]  # logical name -> 0-based column index
    has_table_column: bool
    constraint_cols: dict[str, int] = field(default_factory=dict)  # constraint name -> col index


@dataclass
class SheetReadResult:
    spec: SheetSpec | None = None
    error: RejectionError | None = None


def open_workbook(path: Path) -> Workbook:
    if not path.is_file():
        raise SpecReaderError(f"spec file not found: {path}")
    return load_workbook(filename=str(path), read_only=True, data_only=True)


def list_table_spec_sheets(wb: Workbook, mapping: ColumnMapping) -> list[str]:
    """Return names of visible sheets whose header row matches the required logical columns."""
    out: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        if _locate_header_row(ws, mapping) is not None:
            out.append(sheet_name)
    return out


def read_sheet(wb: Workbook, sheet_name: str, mapping: ColumnMapping) -> SheetReadResult:
    if sheet_name not in wb.sheetnames:
        return _header_not_found(f"sheet {sheet_name!r} not found in workbook")
    ws = wb[sheet_name]

    located = _locate_header_row(ws, mapping)
    if located is None:
        required = [mapping.name.spec_name, mapping.type.spec_name, mapping.nullable.spec_name]
        return _header_not_found(
            f"could not locate a header row in sheet {sheet_name!r}: "
            f"required columns {required} not found in the first {HEADER_SEARCH_DEPTH} rows"
        )
    header_row, headers = located

    col_idx: dict[str, int] = {}
    missing: list[str] = []
    for logical, col_spec in (
        ("name", mapping.name),
        ("type", mapping.type),
        ("description", mapping.description),
        ("nullable", mapping.nullable),
    ):
        idx = find_column(headers, col_spec.spec_name)
        if idx is not None:
            col_idx[logical] = idx
        elif col_spec.column_required:
            missing.append(f"{logical} ({col_spec.spec_name!r})")
        # else: column is `required: false` and absent → skip silently
    if missing:
        return _header_not_found(
            f"sheet {sheet_name!r}: missing required columns: {', '.join(missing)}"
        )

    has_table_column = False
    if mapping.table is not None:
        idx = find_column(headers, mapping.table.spec_name)
        if idx is not None:
            col_idx["table"] = idx
            has_table_column = True
        elif mapping.table.column_required:
            return _header_not_found(
                f"sheet {sheet_name!r}: missing required column: table ({mapping.table.spec_name!r})"
            )

    if mapping.db_name is not None:
        idx = find_column(headers, mapping.db_name.spec_name)
        if idx is not None:
            col_idx["db_name"] = idx
        elif mapping.db_name.column_required:
            return _header_not_found(
                f"sheet {sheet_name!r}: missing required column: db_name ({mapping.db_name.spec_name!r})"
            )

    # Locate any declared constraint columns. Columns with `required: true`
    # (the default) that are missing produce `header_not_found`.
    constraint_cols: dict[str, int] = {}
    constraint_missing: list[str] = []
    for c_name, constraint in mapping.constraints.items():
        idx = find_column(headers, constraint.column.spec_name)
        if idx is not None:
            constraint_cols[c_name] = idx
        elif constraint.column.column_required:
            constraint_missing.append(f"{c_name} ({constraint.column.spec_name!r})")
    if constraint_missing:
        return _header_not_found(
            f"sheet {sheet_name!r}: missing required constraint columns: "
            f"{', '.join(constraint_missing)}"
        )

    return SheetReadResult(
        spec=SheetSpec(
            sheet_name=sheet_name,
            header_row=header_row,
            col_idx=col_idx,
            has_table_column=has_table_column,
            constraint_cols=constraint_cols,
        )
    )


def _header_not_found(message: str) -> SheetReadResult:
    return SheetReadResult(error=RejectionError(kind="header_not_found", message=message))


def find_sheet_by_name(wb: Workbook, name: str) -> tuple[str | None, RejectionError | None]:
    """Tolerant sheet lookup. Normalizes both sides via header_matcher.normalize.

    Returns (sheet_name, None) on a single match.
    Returns (None, keys_sheet_not_found) on zero matches.
    Returns (None, keys_sheet_ambiguous) on two or more.
    """
    target = normalize(name)
    if not target:
        return None, RejectionError(
            kind="keys_sheet_not_found",
            message=f"keys sheet name {name!r} is empty",
        )
    matches = [s for s in wb.sheetnames if normalize(s) == target]
    if not matches:
        return None, RejectionError(
            kind="keys_sheet_not_found",
            message=f"keys sheet {name!r} not found in workbook (visible sheets: {wb.sheetnames})",
        )
    if len(matches) > 1:
        return None, RejectionError(
            kind="keys_sheet_ambiguous",
            message=f"keys sheet name {name!r} matches multiple sheets: {matches}",
        )
    return matches[0], None


def iter_field_rows(wb: Workbook, sheet_spec: SheetSpec) -> Iterator[RawField]:
    ws = wb[sheet_spec.sheet_name]
    # `description` and `nullable` may be absent if the spec declared
    # `required: false`; cells for missing columns become None.
    name_idx = sheet_spec.col_idx.get("name")
    type_idx = sheet_spec.col_idx.get("type")
    desc_idx = sheet_spec.col_idx.get("description")
    null_idx = sheet_spec.col_idx.get("nullable")
    table_idx = sheet_spec.col_idx.get("table")
    db_name_idx = sheet_spec.col_idx.get("db_name")

    mapped_indices = {i for i in (name_idx, type_idx, desc_idx, null_idx, table_idx, db_name_idx) if i is not None}
    for idx in sheet_spec.constraint_cols.values():
        mapped_indices.add(idx)

    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if row_idx <= sheet_spec.header_row:
            continue
        if _row_is_empty(row, mapped_indices):
            continue
        extras = {
            c_name: _cell(row, idx)
            for c_name, idx in sheet_spec.constraint_cols.items()
        }
        yield RawField(
            sheet_row=row_idx,
            name_raw=_cell(row, name_idx),
            type_raw=_cell(row, type_idx),
            description_raw=_cell(row, desc_idx),
            nullable_raw=_cell(row, null_idx),
            table_raw=_cell(row, table_idx) if table_idx is not None else None,
            db_name_raw=_cell(row, db_name_idx) if db_name_idx is not None else None,
            extras=extras,
        )


def _locate_header_row(ws: Worksheet, mapping: ColumnMapping) -> tuple[int, list[str | None]] | None:
    # Only columns flagged `required: true` (the default) contribute to header
    # detection. If a spec author marks e.g. `name.column_required: false`, header
    # discovery falls back to whichever required-true columns remain.
    candidates = [mapping.name, mapping.type, mapping.nullable]
    required_norm = {normalize(c.spec_name) for c in candidates if c.column_required}
    if not required_norm:
        # Pathological config: nothing is required. Treat the first row as the header.
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if first is None:
            return None
        return 1, [None if c is None else str(c) for c in first]
    return locate_header_row(ws, required_norm)
