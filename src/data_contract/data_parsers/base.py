"""File parser base class.

The plugin contract is intentionally narrow: a subclass describes its
file format and how to extract rows + (optionally) the source's
self-declared schema. Everything else -- forcing every column to
`pl.String`, attaching `__source_file__` / `__row_index__` tracking
columns, concatenating across multi-file reads -- is handled by the
concrete `read()` method on this class.

A plugin author with no Polars knowledge can ship a parser that returns
a list of stringified row dicts; the framework wraps it. Plugins that
have a native scan path (CSV via `pl.scan_csv`, Parquet via
`pl.scan_parquet`) return a Polars LazyFrame directly and skip the
materialisation step.

Per-format defaults live in `configs/parsers.yaml`, NOT on the class --
the class declares behavior, the YAML declares values.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterable

from data_contract.errors import ConfigError


# ---------------------------------------------------------------------------
# Public dataclasses returned by parsers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParserSchema:
    """Schema as discovered by the parser from one source file.

    `column_names` -- declared order, when the format carries it (CSV
    header, Excel header, JSON `report_header`, Parquet schema). `None`
    for opaque formats (fixed-width, log files).

    `column_types` -- per-column source-side type string in the format's
    native vocabulary (e.g. `"java.lang.String"`, `"VARCHAR(64)"`, Arrow
    type names). `None` when the format doesn't carry types (CSV).

    The raw strings stay raw here; the parser's `normalize_source_type()`
    translates them to contract `Type` values for the drift check.
    """
    column_names: list[str] | None = None
    column_types: dict[str, str] | None = None


@dataclass
class ParsedFile:
    """A parser's output for one source file.

    `rows`:
      * `Iterable[dict[str, str | None]]` -- the simple path. The framework
        materialises into a Polars DataFrame with `pl.String` dtype on
        every column.
      * `pl.LazyFrame` -- the fast path for parsers with a native scan
        (CSV via `pl.scan_csv`, Parquet via `pl.scan_parquet`). The
        framework still forces every column to `pl.String` and adds the
        tracking columns.

    `schema` -- optional. When present, drift checks
    (`field_names_from_sample`, `field_types_from_sample`) can compare against
    the contract.
    """
    rows: Any                                            # Iterable[dict] | pl.LazyFrame -- avoiding the polars import here
    schema: ParserSchema | None = None


@dataclass(frozen=True)
class ReadResult:
    """What the runner gets back from `FileParser.read()`."""
    frame: Any                                           # pl.LazyFrame -- avoiding the polars import here
    per_file_schemas: list[tuple[str, ParserSchema | None]]


# ---------------------------------------------------------------------------
# FileParser ABC
# ---------------------------------------------------------------------------


class FileParser(ABC):
    """ABC for pluggable file-format parsers.

    Spec params:    declared via PARSER_PARAMS (allowlist of YAML config keys).
    Reads via:      subclass-specific reader (lazy-imported in `parse_file`).
    Multi-file:     handled by the concrete `read()` -- subclasses implement
                    `parse_file()` for ONE path; `read()` loops + concats.

    Subclasses MUST implement `parse_file()`. Subclasses MUST NOT override
    `read()` -- it owns the cross-cutting bookkeeping
    (`__source_file__` / `__row_index__` / `pl.String` coercion / concat)
    and gives that work exactly one home in the codebase.

    Optional class hooks for the schema-drift checks:
      - `SOURCE_TYPE_ALIASES`: dict of source-native type strings ->
        contract `Type` values. Default: empty.
      - `normalize_source_type(s)`: override for regex-style mapping
        (e.g. `"VARCHAR(N)"` -> `"string"`). Default: lookup in
        `SOURCE_TYPE_ALIASES`.

    Per-format defaults live in `configs/parsers.yaml` under a `<name>:`
    block, NOT on this class. The runner loads the YAML once, merges
    `defaults.parser_overrides` + per-table `parser_overrides` on top,
    and hands the resulting dict to `__init__`. Implementations read
    values from `self.params` and may defensively fall back when a key
    is absent (the YAML block is optional).
    """

    name: ClassVar[str] = ""
    extensions: ClassVar[tuple[str, ...]] = ()
    PARSER_PARAMS: ClassVar[tuple[str, ...]] = ()
    SOURCE_TYPE_ALIASES: ClassVar[dict[str, str]] = {}

    params: dict[str, Any]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        raw = dict(params or {})
        unknown = sorted(set(raw) - set(self.PARSER_PARAMS))
        if unknown:
            raise ConfigError(
                f"parser {self.name!r}: unknown config keys {unknown}; "
                f"accepted: {list(self.PARSER_PARAMS)}"
            )
        self.params = raw

    # ------------------------------------------------------------------
    # Plugin entry point
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_file(
        self, path: Path, *, table_name_hint: str | None = None
    ) -> ParsedFile:
        """Read ONE source file. Return a `ParsedFile` carrying rows and
        an optional `ParserSchema`. The framework's `read()` handles
        everything else.

        `table_name_hint` is the contract's table key, supplied so parsers
        that have a notion of "section within the file" (e.g. Excel
        sheets) can use it as a fallback target. Parsers without that
        notion ignore it.
        """

    # ------------------------------------------------------------------
    # Type translation hook for the drift checks
    # ------------------------------------------------------------------

    def normalize_source_type(self, source_type: str) -> str | None:
        """Map a source-side type string to a contract `Type` value (one
        of the canonical strings registered in `configs/types.yaml`), or
        `None` if the mapping is unknown.

        Default behaviour: look up `SOURCE_TYPE_ALIASES`. Subclasses
        override for regex-style mapping (e.g. `"VARCHAR(N)"` ->
        `"string"` for any N).
        """
        return self.SOURCE_TYPE_ALIASES.get(source_type)

    # ------------------------------------------------------------------
    # Concrete read() -- subclasses MUST NOT override
    # ------------------------------------------------------------------

    def read(
        self, paths: list[Path], *, table_name_hint: str | None = None
    ) -> ReadResult:
        """Read all `paths` and return a unified LazyFrame plus the
        per-file schemas.

        Bookkeeping handled here so plugin authors never have to:
          - Every data column forced to `pl.String`.
          - `__source_file__` (filename) and `__row_index__` (1-based per
            file) added as tracking columns.
          - Multi-file frames concatenated via `diagonal_relaxed` so
            differing columns across files become null in the union.
        """
        if not paths:
            raise ValueError(f"parser {self.name!r}: read called with no paths")

        # Polars lazy-imported here so importing this module doesn't
        # require the validate-data extras.
        import polars as pl

        per_file_lf: list[Any] = []
        per_file_schemas: list[tuple[str, ParserSchema | None]] = []

        for path in paths:
            parsed = self.parse_file(path, table_name_hint=table_name_hint)
            lf = self._materialise(parsed, pl)
            lf = self._attach_tracking(lf, path, pl)
            per_file_lf.append(lf)
            per_file_schemas.append((path.name, parsed.schema))

        if len(per_file_lf) == 1:
            frame = per_file_lf[0]
        else:
            frame = pl.concat(per_file_lf, how="diagonal_relaxed")

        return ReadResult(frame=frame, per_file_schemas=per_file_schemas)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _materialise(parsed: ParsedFile, pl_module) -> Any:
        """Coerce `parsed.rows` to a `pl.LazyFrame` with every column as
        `pl.String`. Accepts a LazyFrame (passes through after a String
        cast) or an iterable of dicts (materialises)."""
        rows = parsed.rows
        if isinstance(rows, pl_module.LazyFrame):
            lf = rows
            # Force every existing column to String. The reserved tracking
            # columns are added later, so they're not yet present.
            cols = lf.collect_schema().names()
            return lf.with_columns(
                [pl_module.col(c).cast(pl_module.String, strict=False) for c in cols]
            )
        # Iterable-of-dicts path. Materialise via DataFrame and pin every
        # column to String. The column order is taken from the parser's
        # schema when available so empty data still yields the declared
        # columns; otherwise the dict-key order from the first row wins.
        rows_list = list(rows)
        declared_cols: list[str] = []
        if parsed.schema is not None and parsed.schema.column_names:
            declared_cols.extend(parsed.schema.column_names)
        seen = set(declared_cols)
        for row in rows_list:
            for k in row:
                if k not in seen:
                    declared_cols.append(k)
                    seen.add(k)
        schema = {c: pl_module.String for c in declared_cols}
        if rows_list:
            df = pl_module.DataFrame(rows_list, schema_overrides=schema)
            missing = [c for c in declared_cols if c not in df.columns]
            if missing:
                df = df.with_columns(
                    [pl_module.lit(None).cast(pl_module.String).alias(c) for c in missing]
                )
            df = df.select(declared_cols)
        else:
            df = pl_module.DataFrame({c: [] for c in declared_cols}, schema=schema)
        return df.lazy()

    @staticmethod
    def _attach_tracking(lf: Any, path: Path, pl_module) -> Any:
        """Add `__row_index__` (1-based per file) and `__source_file__`
        (filename)."""
        return lf.with_row_index(name="__row_index__", offset=1).with_columns(
            pl_module.lit(path.name).alias("__source_file__"),
        )
