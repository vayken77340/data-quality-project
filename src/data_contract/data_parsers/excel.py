"""Excel parser using `python-calamine`.

Calamine is the minimum-possible xlsx decoder: it opens the file and yields
cells as Python objects. We do NOT route through `pl.read_excel` because that
re-introduces Polars dtype inference -- defeating the "Excel is just a format"
principle. The parser hands every cell to `_to_string` and the contract layer
(check_type_coercion + value_parsers) is the only authority on what a valid
INTEGER / DATE / BOOLEAN string looks like.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data_contract.data_parsers.base import FileParser, ParsedFile, ParserSchema
from data_contract.errors import ConfigError


class ExcelParser(FileParser):
    """Read one Excel workbook into a list of stringified row dicts.

    Spec params:    header_row, null_tokens, sheet_name, match_header.
                    `match_header` (default False) decides how data columns
                    bind to contract fields:
                      * False -- POSITIONAL: the i-th data column is the
                        i-th contract field (rename happens per file before
                        the framework's multi-file concat, so workbooks
                        whose sheet headers disagree still align).
                      * True -- NAME-BASED: data columns keep their header
                        names; downstream binding is by name.
                    Either way, the actual sheet header is recorded in
                    `ParserSchema.column_names` so the drift checks can
                    compare against the contract independently of how the
                    parser bound rows to columns.
    Reads via:      python-calamine (Rust-based xlsx reader). Every cell is
                    stringified via `_to_string` -- no Excel-specific
                    rendering, no format-string interpretation. Sheet
                    selection precedence (per file):
                      1. explicit `sheet_name` from params (validation.yaml).
                      2. a sheet whose name matches the runner-supplied
                         `table_name_hint` (i.e. the contract's table key)
                         when such a sheet exists in the workbook.
                      3. fallback to the first sheet.
    Multi-file:     handled by `FileParser.read()`.

    Schema:         column_names always the original sheet header. column_types
                    is None -- Excel cell types are too fuzzy to declare
                    confidently (a column may mix numbers, formulas, blanks).
    """

    name = "excel"
    extensions = (".xlsx", ".xls")
    PARSER_PARAMS = ("header_row", "null_tokens", "sheet_name", "match_header")
    # No DEFAULTS classvar -- per-format defaults live in `configs/parsers.yaml`.

    def parse_file(
        self, path: Path, *, table_name_hint: str | None = None,
    ) -> ParsedFile:
        from python_calamine import CalamineWorkbook

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])
        explicit_sheet = self.params.get("sheet_name")
        match_header = bool(self.params.get("match_header", False))

        wb = CalamineWorkbook.from_path(str(path))
        resolved_sheet = _resolve_sheet_name(
            wb, explicit=explicit_sheet, table_name_hint=table_name_hint
        )
        sheet = wb.get_sheet_by_name(resolved_sheet)
        raw_rows = sheet.to_python(skip_empty_area=False)

        sliced = raw_rows[skip:]
        if not sliced:
            return ParsedFile(
                rows=[],
                schema=ParserSchema(column_names=[], column_types=None),
            )

        header = [str(c) if c is not None else "" for c in sliced[0]]
        effective_columns = (
            header if match_header
            else _positional_column_keys(header, self.contract_field_names)
        )

        data_rows = sliced[1:]
        null_set = set(null_tokens)
        rows: list[dict[str, str | None]] = []
        for row in data_rows:
            # Pad / truncate to header width; calamine returns ragged rows
            # when trailing cells are empty.
            padded = list(row) + [None] * (len(header) - len(row))
            out: dict[str, str | None] = {}
            for i, key in enumerate(effective_columns):
                value = _to_string(padded[i])
                if value in null_set:
                    value = None
                out[key] = value
            rows.append(out)

        return ParsedFile(
            rows=rows,
            schema=ParserSchema(column_names=header, column_types=None),
        )


def _positional_column_keys(
    header: list[str], contract_field_names: list[str] | None,
) -> list[str]:
    """Return the per-column key under which row values are emitted in positional mode.

    Each header column is replaced with the matching contract field name (by
    index). Columns beyond the contract's field count keep their header name
    -- the `extra_column` check downstream catches them. Raises ConfigError
    if a positional rename would collide with another existing header.
    """
    if not contract_field_names:
        return list(header)
    n = min(len(header), len(contract_field_names))
    # Collision: a contract name written into the first n positions also
    # appears as an UN-renamed header at position >= n.
    duplicate_targets = sorted(set(contract_field_names[:n]) & set(header[n:]))
    if duplicate_targets:
        raise ConfigError(
            f"excel parser: positional rename would create duplicate columns "
            f"{duplicate_targets}. The sheet already has columns with these "
            f"names AT DIFFERENT POSITIONS than the contract declares. "
            f"Either reorder the sheet, set `match_header: true` for this "
            f"table, or rename the colliding contract fields."
        )
    return list(contract_field_names[:n]) + list(header[n:])


def _to_string(value: Any) -> str | None:
    """Render a calamine cell value as a string.

    The only Excel-aware concession the parser makes: xlsx stores all numbers
    as IEEE 754 doubles, so a cell author-typed as `42` comes back as the
    float `42.0`. We downcast whole-number floats to int before stringifying
    so the contract's INTEGER regex isn't gratuitously broken by the storage
    format. Everything else goes through plain `str()`.
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value
    return str(value)


def _resolve_sheet_name(
    wb,
    *,
    explicit: str | None,
    table_name_hint: str | None,
) -> str:
    """Apply the three-step precedence: explicit -> table_name_hint -> first sheet."""
    names = list(wb.sheet_names)
    if explicit:
        return explicit
    if table_name_hint and table_name_hint in names:
        return table_name_hint
    return names[0]
