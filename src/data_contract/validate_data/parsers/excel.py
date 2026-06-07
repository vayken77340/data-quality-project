"""Excel parser using Polars `read_excel`.

Always reads the first (active) sheet of the workbook. Sample data files in
this project are single-sheet by convention; multi-sheet workbooks have their
secondary sheets ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data_contract.validate_data.parsers.base import FileParser


class ExcelParser(FileParser):
    """Read Excel workbooks (one or more) into a single Polars LazyFrame.

    Spec params:    header_row, null_tokens, sheet_name.
    Reads via:      polars.read_excel (eager — Polars does not lazy-scan xlsx).
                    Sheet selection precedence (per file):
                      1. explicit `sheet_name` from params (validation.yaml).
                      2. a sheet whose name matches the runner-supplied
                         `table_name_hint` (i.e. the contract's table key) when
                         such a sheet exists in the workbook.
                      3. fallback to the first (active) sheet.
    Multi-file:     concat with `__source_file__` and `__row_index__` columns
                    added per source frame.
    """

    name = "excel"
    extensions = (".xlsx", ".xls")
    PARSER_PARAMS = ("header_row", "null_tokens", "sheet_name")
    DEFAULTS = {"header_row": 1, "null_tokens": [""]}

    def read(self, paths: list[Path], *, table_name_hint: str | None = None) -> Any:
        import polars as pl
        from openpyxl import load_workbook

        if not paths:
            raise ValueError("ExcelParser.read called with no paths")

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])
        explicit_sheet = self.params.get("sheet_name")

        frames = []
        for path in paths:
            read_kwargs: dict[str, Any] = {
                "read_options": {"header_row": skip},
            }
            resolved_sheet = explicit_sheet
            if not resolved_sheet and table_name_hint:
                # Peek at the workbook's sheet names cheaply via openpyxl.
                wb = load_workbook(str(path), read_only=True)
                try:
                    if table_name_hint in wb.sheetnames:
                        resolved_sheet = table_name_hint
                finally:
                    wb.close()
            if resolved_sheet:
                read_kwargs["sheet_name"] = resolved_sheet
            else:
                read_kwargs["sheet_id"] = 1   # final fallback: first (active) sheet
            df = pl.read_excel(str(path), **read_kwargs)
            # Map configured null tokens to actual nulls.
            for col in df.columns:
                if df[col].dtype == pl.String:
                    df = df.with_columns(
                        pl.when(pl.col(col).is_in(null_tokens))
                        .then(None)
                        .otherwise(pl.col(col))
                        .alias(col)
                    )
            lf = df.lazy().with_row_index(name="__row_index__", offset=1).with_columns(
                pl.lit(path.name).alias("__source_file__"),
            )
            frames.append(lf)
        if len(frames) == 1:
            return frames[0]
        return pl.concat(frames, how="diagonal_relaxed")
