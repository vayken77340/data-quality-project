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

    Spec params:    header_row, null_tokens.
    Reads via:      polars.read_excel (eager — Polars does not lazy-scan xlsx).
                    Always reads the first (active) sheet of the workbook.
    Multi-file:     concat with `__source_file__` and `__row_index__` columns
                    added per source frame.
    """

    name = "excel"
    extensions = (".xlsx", ".xls")
    PARSER_PARAMS = ("header_row", "null_tokens")
    DEFAULTS = {"header_row": 1, "null_tokens": [""]}

    def read(self, paths: list[Path]) -> Any:
        import polars as pl

        if not paths:
            raise ValueError("ExcelParser.read called with no paths")

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])

        frames = []
        for path in paths:
            df = pl.read_excel(
                str(path),
                sheet_id=1,  # always the first sheet
                read_options={
                    "header_row": skip,
                },
            )
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
