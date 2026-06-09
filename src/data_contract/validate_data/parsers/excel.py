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

from data_contract.validate_data.parsers.base import FileParser


class ExcelParser(FileParser):
    """Read Excel workbooks (one or more) into a single Polars LazyFrame.

    Spec params:    header_row, null_tokens, sheet_name.
    Reads via:      python-calamine (Rust-based xlsx reader). Every cell is
                    stringified via `_to_string` -- no Excel-specific
                    rendering, no format-string interpretation. Sheet
                    selection precedence (per file):
                      1. explicit `sheet_name` from params (validation.yaml).
                      2. a sheet whose name matches the runner-supplied
                         `table_name_hint` (i.e. the contract's table key)
                         when such a sheet exists in the workbook.
                      3. fallback to the first sheet.
    Multi-file:     concat with `__source_file__` and `__row_index__` columns
                    added per source frame.
    """

    name = "excel"
    extensions = (".xlsx", ".xls")
    PARSER_PARAMS = ("header_row", "null_tokens", "sheet_name")
    DEFAULTS = {"header_row": 1, "null_tokens": [""]}

    def read(self, paths: list[Path], *, table_name_hint: str | None = None) -> Any:
        import polars as pl
        from python_calamine import CalamineWorkbook

        if not paths:
            raise ValueError("ExcelParser.read called with no paths")

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])
        explicit_sheet = self.params.get("sheet_name")

        frames = []
        for path in paths:
            wb = CalamineWorkbook.from_path(str(path))
            resolved_sheet = _resolve_sheet_name(
                wb, explicit=explicit_sheet, table_name_hint=table_name_hint
            )
            sheet = wb.get_sheet_by_name(resolved_sheet)
            rows = sheet.to_python(skip_empty_area=False)

            sliced = rows[skip:]
            if not sliced:
                df = pl.DataFrame({}, schema={})
            else:
                header = [str(c) if c is not None else "" for c in sliced[0]]
                data_rows = sliced[1:]
                columns: dict[str, list[str | None]] = {h: [] for h in header}
                for row in data_rows:
                    # Pad / truncate to header width; calamine returns ragged
                    # rows when trailing cells are empty.
                    padded = list(row) + [None] * (len(header) - len(row))
                    for i, h in enumerate(header):
                        columns[h].append(_to_string(padded[i]))
                schema = {h: pl.String for h in header}
                df = pl.DataFrame(columns, schema=schema)

            # Map configured null tokens to actual nulls (every column is
            # String here, so this is uniform).
            if null_tokens:
                df = df.with_columns(
                    pl.when(pl.col(pl.String).is_in(null_tokens))
                    .then(None)
                    .otherwise(pl.col(pl.String))
                    .name.keep()
                )

            lf = df.lazy().with_row_index(name="__row_index__", offset=1).with_columns(
                pl.lit(path.name).alias("__source_file__"),
            )
            frames.append(lf)
        if len(frames) == 1:
            return frames[0]
        return pl.concat(frames, how="diagonal_relaxed")


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
