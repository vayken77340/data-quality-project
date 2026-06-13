"""Keys sheet: centralized primary-key / foreign-key declarations.

The spec workbook ships one dedicated "keys" sheet (name configured in
defaults.yaml under `keys.sheet_name`). The generator reads it once per
epic, then enriches each generated FieldContract with:

  - `primary_key: true` for fields that appear in the row's PK list.
  - `foreign_key: {table: T, column: C}` for fields in the row's FK list,
    where T is found by searching the keys sheet for a row whose PK list
    contains C.

The keys sheet itself is structurally validated; failures emit per-table
rejections through the existing quarantine pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openpyxl.workbook.workbook import Workbook

from data_contract.generation.config import KeysSpec
from data_contract.contract import FieldContract
from data_contract.errors import RejectionError
from data_contract.generation.header_matcher import find_column, normalize
from data_contract.generation.spec_reader import HEADER_SEARCH_DEPTH, find_sheet_by_name


# ---------------------------------------------------------------------------
# Runtime dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeysRow:
    sheet_row: int
    table_name: str
    primary_keys: list[str]
    foreign_keys: list[str]


@dataclass
class KeysData:
    rows: list[KeysRow] = field(default_factory=list)
    errors: list[RejectionError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def rows_for_table(self, table: str) -> list[KeysRow]:
        return [r for r in self.rows if r.table_name == table]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_separated(raw: object, separator: str) -> list[str]:
    """Split a raw cell value on `separator`, strip whitespace from each piece,
    and drop empty pieces. Returns [] if the cell is None/blank."""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(separator)]
    return [p for p in parts if p]


def build_pk_index(rows: list[KeysRow]) -> dict[str, set[str]]:
    """`pk_column_name -> set[table_name]`. A column declared as PK on multiple
    tables shows up with multiple entries — the caller treats that as ambiguous
    when resolving an FK against it."""
    index: dict[str, set[str]] = {}
    for r in rows:
        for col in r.primary_keys:
            index.setdefault(col, set()).add(r.table_name)
    return index


# ---------------------------------------------------------------------------
# Sheet reading
# ---------------------------------------------------------------------------


def read_keys_sheet(wb: Workbook, keys_spec: KeysSpec) -> KeysData:
    """Read the keys sheet end-to-end. Errors accumulate; a non-empty `errors`
    list means the data shouldn't be used for enrichment."""
    out = KeysData()

    sheet_name, sheet_err = find_sheet_by_name(wb, keys_spec.sheet_name)
    if sheet_err is not None:
        out.errors.append(sheet_err)
        return out
    ws = wb[sheet_name]
    cm = keys_spec.column_mapping

    # Locate header row (depth-N scan, matching the table-sheet behavior).
    required_norm = {normalize(cm.table_name.spec_name), normalize(cm.primary_key.spec_name)}
    located: tuple[int, list[str | None]] | None = None
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=HEADER_SEARCH_DEPTH, values_only=True), start=1
    ):
        present = {normalize(c) for c in row if c is not None}
        if required_norm.issubset(present):
            located = row_idx, [None if c is None else str(c) for c in row]
            break

    if located is None:
        out.errors.append(RejectionError(
            kind="header_not_found",
            message=(
                f"keys sheet {sheet_name!r}: could not locate a header row "
                f"with required columns {sorted(required_norm)} in the first {HEADER_SEARCH_DEPTH} rows"
            ),
        ))
        return out

    header_row, headers = located

    # Find each declared column. Columns whose `required: true` (default) are
    # missing -> header_not_found.
    table_name_idx = find_column(headers, cm.table_name.spec_name)
    pk_idx = find_column(headers, cm.primary_key.spec_name)
    fk_idx = find_column(headers, cm.foreign_key.spec_name) if cm.foreign_key is not None else None
    # `comments` index isn't needed (never carried into the contract); validate header presence only.

    missing: list[str] = []
    if table_name_idx is None and cm.table_name.column_required:
        missing.append(f"table_name ({cm.table_name.spec_name!r})")
    if pk_idx is None and cm.primary_key.column_required:
        missing.append(f"primary_key ({cm.primary_key.spec_name!r})")
    if cm.foreign_key is not None and fk_idx is None and cm.foreign_key.column_required:
        missing.append(f"foreign_key ({cm.foreign_key.spec_name!r})")
    if cm.comments is not None and cm.comments.column_required:
        if find_column(headers, cm.comments.spec_name) is None:
            missing.append(f"comments ({cm.comments.spec_name!r})")
    if missing:
        out.errors.append(RejectionError(
            kind="header_not_found",
            message=f"keys sheet {sheet_name!r}: missing required columns: {', '.join(missing)}",
        ))
        return out

    # Iterate rows.
    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if row_idx <= header_row:
            continue
        if _row_is_empty(row, [i for i in (table_name_idx, pk_idx, fk_idx) if i is not None]):
            continue

        table_raw = _cell(row, table_name_idx)
        pk_raw = _cell(row, pk_idx)
        fk_raw = _cell(row, fk_idx) if fk_idx is not None else None

        # table_name (value_required honored)
        if table_raw is None or str(table_raw).strip() == "":
            if cm.table_name.value_required:
                out.errors.append(RejectionError(
                    kind="missing_mandatory",
                    sheet_row=row_idx,
                    column=cm.table_name.spec_name,
                    field="table_name",
                    message=(
                        f"keys sheet {sheet_name!r} row {row_idx}: "
                        f"column {cm.table_name.spec_name!r} (table_name) requires a value but the cell is empty"
                    ),
                ))
            continue
        table_name = str(table_raw).strip()

        # primary_key (must yield >=1 after split when value_required)
        primary_keys = split_separated(pk_raw, cm.primary_key.separator)
        if not primary_keys:
            if cm.primary_key.value_required:
                out.errors.append(RejectionError(
                    kind="missing_mandatory",
                    sheet_row=row_idx,
                    column=cm.primary_key.spec_name,
                    field="primary_key",
                    value=pk_raw,
                    message=(
                        f"keys sheet {sheet_name!r} row {row_idx} (table {table_name!r}): "
                        f"column {cm.primary_key.spec_name!r} (primary_key) requires a value but the cell is empty"
                    ),
                ))
            continue

        # foreign_key (optional)
        foreign_keys: list[str] = []
        if cm.foreign_key is not None and fk_idx is not None:
            foreign_keys = split_separated(fk_raw, cm.foreign_key.separator)

        out.rows.append(KeysRow(
            sheet_row=row_idx,
            table_name=table_name,
            primary_keys=primary_keys,
            foreign_keys=foreign_keys,
        ))

    return out


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich_field_contract_list(
    fields: list[FieldContract],
    table: str,
    keys_rows_for_table: list[KeysRow],
    pk_index: dict[str, set[str]],
    *,
    fk_allow_violations: bool = False,
) -> tuple[list[FieldContract], list[RejectionError], list[RejectionError]]:
    """Apply PK flags and resolved FKs to the field list in-place.

    Returns `(fields, errors, fk_warnings)`. When `fk_allow_violations` is True,
    FK resolution errors (`unknown_foreign_key_target`,
    `ambiguous_foreign_key_target`) land in `fk_warnings` rather than `errors`;
    the affected fields just don't get the `foreign_key` annotation.

    Multiple keys rows for the same table merge additively (the spec author may
    split PK and FK declarations across rows).
    """
    errors: list[RejectionError] = []
    fk_warnings: list[RejectionError] = []
    by_name: dict[str, FieldContract] = {f.name: f for f in fields}

    # Aggregate PK and FK columns across all rows for this table.
    pk_cols: set[str] = set()
    fk_cols: set[str] = set()
    for r in keys_rows_for_table:
        pk_cols.update(r.primary_keys)
        fk_cols.update(r.foreign_keys)

    def _add_fk_error(err: RejectionError) -> None:
        (fk_warnings if fk_allow_violations else errors).append(err)

    # Primary keys.
    for col in sorted(pk_cols):
        f = by_name.get(col)
        if f is None:
            errors.append(RejectionError(
                kind="unknown_pk_field",
                field=col,
                value=col,
                message=(
                    f"keys sheet declares column {col!r} as a primary key of table {table!r}, "
                    f"but no field with that name exists in the table"
                ),
            ))
            continue
        if f.nullable is True:
            errors.append(RejectionError(
                kind="nullable_primary_key",
                field=col,
                value=col,
                message=(
                    f"field {col!r} on table {table!r} is declared as a primary key "
                    f"but its `nullable` flag is true; primary key columns must not be nullable"
                ),
            ))
        f.primary_key = True

    # Foreign keys.
    for col in sorted(fk_cols):
        f = by_name.get(col)
        if f is None:
            _add_fk_error(RejectionError(
                kind="unknown_foreign_key_target",
                field=col,
                value=col,
                message=(
                    f"keys sheet declares column {col!r} as a foreign key of table {table!r}, "
                    f"but no field with that name exists in the table"
                ),
            ))
            continue
        targets = pk_index.get(col, set()) - {table}
        if not targets:
            _add_fk_error(RejectionError(
                kind="unknown_foreign_key_target",
                field=col,
                value=col,
                message=(
                    f"foreign key column {col!r} on table {table!r} does not match any primary key "
                    f"declared in the keys sheet"
                ),
            ))
            continue
        if len(targets) > 1:
            _add_fk_error(RejectionError(
                kind="ambiguous_foreign_key_target",
                field=col,
                value=sorted(targets),
                message=(
                    f"foreign key column {col!r} on table {table!r} is ambiguous: "
                    f"matches primary keys of multiple tables: {sorted(targets)}"
                ),
            ))
            continue
        target_table = next(iter(targets))
        f.foreign_key = {"table": target_table, "column": col}

    return fields, errors, fk_warnings


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _cell(row: tuple, idx: int | None) -> object | None:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _row_is_empty(row: tuple, indices: list[int]) -> bool:
    for i in indices:
        v = _cell(row, i)
        if v is not None and str(v).strip() != "":
            return False
    return True
