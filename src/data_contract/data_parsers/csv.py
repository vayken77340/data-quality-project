"""CSV parser using Polars `scan_csv` (native lazy scan path)."""

from __future__ import annotations

from pathlib import Path

from data_contract.data_parsers.base import FileParser, ParsedFile, ParserSchema
from data_contract.errors import ConfigError


class CsvParser(FileParser):
    """Read a CSV file into a Polars LazyFrame.

    Spec params:    encoding, delimiter, header_row, null_tokens, quote_char,
                    match_header.
                    `match_header` (default False) decides how data columns
                    bind to contract fields:
                      * False -- POSITIONAL: the i-th data column is the
                        i-th contract field (rename happens per file before
                        the framework's multi-file concat, so CSVs whose
                        headers disagree still align).
                      * True -- NAME-BASED: data columns keep their header
                        names; downstream binding is by name.
                    Either way, the actual file header is recorded in
                    `ParserSchema.column_names` so the drift checks
                    (`field_names_from_sample` / `field_types_from_sample`)
                    can compare against the contract independently of how
                    the parser bound rows to columns.
    Reads via:      polars.scan_csv (lazy; predicate pushdown supported).
                    Returns the LazyFrame directly via `ParsedFile.rows`;
                    the framework handles tracking columns and multi-file
                    concat.
    Multi-file:     handled by `FileParser.read()` (concat across paths).

    Schema:         column_names always the original CSV header (never the
                    post-rename values). column_types is None -- CSV doesn't
                    carry type information.
    """

    name = "csv"
    extensions = (".csv",)
    PARSER_PARAMS = (
        "encoding", "delimiter", "header_row", "null_tokens", "quote_char",
        "match_header",
    )
    # No DEFAULTS classvar -- per-format defaults live in `configs/parsers.yaml`.

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
        match_header = bool(self.params.get("match_header", False))

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

        # Capture the actual CSV header for the ParserSchema BEFORE any
        # positional rename -- the drift check needs the original file
        # header regardless of how this parser bound rows to columns.
        header_names = lf.collect_schema().names()

        if not match_header:
            lf = _apply_positional_rename(lf, header_names, self.contract_field_names)

        return ParsedFile(
            rows=lf,
            schema=ParserSchema(column_names=header_names, column_types=None),
        )


def _apply_positional_rename(lf, header_names, contract_field_names):
    """Rename the i-th data column to `contract_field_names[i]` (positional mode).

    No-op when `contract_field_names` is None (parser used outside the
    contract-driven runner) or when each header column already carries its
    contract name. Raises `ConfigError` when the rename would collide with
    another existing column at a different position -- Polars would silently
    produce duplicates otherwise.
    """
    if not contract_field_names:
        return lf
    rename_map = {
        header_names[i]: contract_field_names[i]
        for i in range(min(len(header_names), len(contract_field_names)))
        if header_names[i] != contract_field_names[i]
    }
    if not rename_map:
        return lf
    duplicate_targets = sorted(
        set(rename_map.values()) & (set(header_names) - set(rename_map.keys()))
    )
    if duplicate_targets:
        raise ConfigError(
            f"csv parser: positional rename would create duplicate columns "
            f"{duplicate_targets}. The file already has columns with these "
            f"names AT DIFFERENT POSITIONS than the contract declares. "
            f"Either reorder the file, set `match_header: true` for this "
            f"table, or rename the colliding contract fields."
        )
    return lf.rename(rename_map)
