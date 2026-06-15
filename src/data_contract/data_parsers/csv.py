"""CSV parser using Polars `scan_csv` (native lazy scan path)."""

from __future__ import annotations

from pathlib import Path

from data_contract.data_parsers.base import FileParser, ParsedFile, ParserSchema


class CsvParser(FileParser):
    """Read a CSV file into a Polars LazyFrame.

    Spec params:    encoding, delimiter, header_row, null_tokens, quote_char.
                    The generic `field_matching_policy` (default
                    "positional" -- see `default_field_matching_policy`
                    below) is added by the base class; column binding is
                    handled uniformly in `FileParser._apply_field_matching`
                    after `parse_file` returns. This parser just emits the
                    LazyFrame with the actual CSV header as its column names.
    Reads via:      polars.scan_csv (lazy; predicate pushdown supported).
                    Returns the LazyFrame directly via `ParsedFile.rows`;
                    the framework handles tracking columns and multi-file
                    concat.
    Multi-file:     handled by `FileParser.read()` (concat across paths).

    Schema:         column_names = the actual CSV header (regardless of
                    which `field_matching_policy` later remaps them);
                    column_types is None -- CSV doesn't carry type info.
    """

    name = "csv"
    extensions = (".csv",)
    PARSER_PARAMS = (
        "encoding", "delimiter", "header_row", "null_tokens", "quote_char",
    )
    # CSV headers are textual and often stale; positional is the safe default.
    default_field_matching_policy = "positional"

    def parse_file(
        self, path: Path, *, table_name_hint: str | None = None,
    ) -> ParsedFile:
        # CSV files have no concept of named sections -- the hint is ignored.
        # Lazy import: Polars is only loaded when validate-data actually runs.
        import polars as pl

        skip = max(0, int(self.params["header_row"]) - 1)
        null_tokens = list(self.params["null_tokens"])
        # Polars expects `encoding` as either "utf8" (with fast path) or "utf8-lossy".
        encoding = str(self.params["encoding"]).lower().replace("-", "")
        encoding = "utf8" if encoding == "utf8" else "utf8-lossy"

        # `infer_schema=False` forces every data column to pl.String. The
        # contract layer is the only authority on type validity (see
        # check_type_coercion + normalize_typed_column). We pull null-token
        # handling out of scan_csv so we can apply it ourselves after the
        # column is guaranteed to be String -- keeps the everything-as-string
        # invariant intact at the boundary.
        lf = pl.scan_csv(
            str(path),
            separator=self.params["delimiter"],
            has_header=True,
            skip_rows=skip,
            quote_char=self.params["quote_char"],
            encoding=encoding,
            infer_schema=False,
        )
        if null_tokens:
            lf = lf.with_columns(
                pl.when(pl.col(pl.String).is_in(null_tokens))
                .then(None)
                .otherwise(pl.col(pl.String))
                .name.keep()
            )

        # Column names are taken straight from the CSV header. The
        # `field_matching_policy` step (in base `read()`) decides how
        # those map to contract field names -- nothing format-specific
        # to do here.
        header_names = lf.collect_schema().names()
        return ParsedFile(
            rows=lf,
            schema=ParserSchema(column_names=header_names, column_types=None),
        )
