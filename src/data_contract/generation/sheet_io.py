"""Shared workbook-sheet plumbing.

Three call sites (`spec_reader`, `keys`, `joins`) all need the same primitives:

  * locate a header row within the first N rows of a sheet by checking that
    a set of required (normalized) column names is present,
  * safely read a cell by column index without IndexError on ragged rows,
  * decide whether a row carries any non-blank content among a given set of
    column indices.

Keeping them here means the depth-N scan logic, the ragged-row defensiveness,
and the "what counts as empty" rule each have exactly one home in the codebase.

Each caller still builds its own `header_not_found` RejectionError because
the messages legitimately differ ("sheet X" vs "keys sheet X" vs
"joins sheet X") — only the loop body is shared.
"""

from __future__ import annotations

from typing import Iterable

from openpyxl.worksheet.worksheet import Worksheet

from data_contract.generation.header_matcher import normalize


HEADER_SEARCH_DEPTH = 5  # scan first N rows of a sheet looking for the header


def cell(row: tuple, idx: int | None) -> object | None:
    """Read `row[idx]` defensively. Returns None for `idx is None` or when the
    row is shorter than `idx + 1` (openpyxl returns ragged rows when trailing
    cells are empty)."""
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def row_is_empty(row: tuple, indices: Iterable[int]) -> bool:
    """True iff every cell named by `indices` is None or whitespace-only."""
    for i in indices:
        v = cell(row, i)
        if v is not None and str(v).strip() != "":
            return False
    return True


def locate_header_row(
    ws: Worksheet,
    required_norm: set[str],
    *,
    search_depth: int = HEADER_SEARCH_DEPTH,
) -> tuple[int, list[str | None]] | None:
    """Scan the first `search_depth` rows looking for one that carries every
    name in `required_norm` (matched against the row's cells after the same
    `header_matcher.normalize` pass).

    Returns `(1-based header row index, headers_as_strings)` on success or
    `None` when no row in the window has the full set. Callers translate the
    `None` into their own format-specific `header_not_found` RejectionError.
    """
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=search_depth, values_only=True), start=1,
    ):
        present = {normalize(c) for c in row if c is not None}
        if required_norm.issubset(present):
            return row_idx, [None if c is None else str(c) for c in row]
    return None
