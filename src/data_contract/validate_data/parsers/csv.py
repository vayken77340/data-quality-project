"""CSV parser using Polars `scan_csv`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data_contract.validate_data.parsers.base import FileParser


class CsvParser(FileParser):
    """Read CSV files (one or more) into a single Polars LazyFrame.

    Spec params:    encoding, delimiter, header_row, null_tokens, quote_char.
    Reads via:      polars.scan_csv (lazy; predicate pushdown supported).
    Multi-file:     concat with `__source_file__` and `__row_index__` columns
                    added per source frame.
    """

    name = "csv"
    extensions = (".csv",)
    PARSER_PARAMS = ("encoding", "delimiter", "header_row", "null_tokens", "quote_char")
    DEFAULTS = {
        "encoding": "utf-8",
        "delimiter": ",",
        "header_row": 1,
        "null_tokens": [""],
        "quote_char": '"',
    }

    def read(self, paths: list[Path]) -> Any:
        # Lazy import — Polars is only loaded when validate-data actually runs.
        import polars as pl

        if not paths:
            raise ValueError("CsvParser.read called with no paths")

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])
        # Polars expects `encoding` as either "utf8" (with fast path) or "utf8-lossy".
        # For other encodings we read bytes ourselves; for now use utf8 fast path.
        encoding = str(self.params["encoding"]).lower().replace("-", "")
        encoding = "utf8" if encoding == "utf8" else "utf8-lossy"

        frames = []
        for path in paths:
            lf = pl.scan_csv(
                str(path),
                separator=self.params["delimiter"],
                has_header=True,
                skip_rows=skip,
                null_values=null_tokens,
                quote_char=self.params["quote_char"],
                encoding=encoding,
                infer_schema_length=10000,
            )
            lf = lf.with_row_index(name="__row_index__", offset=1).with_columns(
                pl.lit(path.name).alias("__source_file__"),
            )
            frames.append(lf)
        if len(frames) == 1:
            return frames[0]
        return pl.concat(frames, how="diagonal_relaxed")
