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

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from difflib import SequenceMatcher
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
# Field-matching policy
# ---------------------------------------------------------------------------


_VALID_FIELD_MATCHING_POLICIES = frozenset({"positional", "exact", "similarity"})

# Collapse runs of non-alphanumeric characters into a single space so
# "User_ID", "User ID", "user-id" all normalize to "user id".
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_for_matching(s: str) -> str:
    return _NORMALIZE_RE.sub(" ", s.lower()).strip()


def _similarity_score(a: str, b: str) -> float:
    """Compute the matching score between two column names.

    Score is `max(char_ratio, token_jaccard)` over the normalized form:
      * `char_ratio` -- difflib.SequenceMatcher.ratio() on the normalized
        strings. Catches typos and tight character-level variation.
      * `token_jaccard` -- |A∩B| / |A∪B| over the whitespace-split token
        sets. Catches reordered tokens ("user id" vs "id user") and partial
        overlap.
    Taking the max means either signal can validate a match. This works
    well for case / spacing / punctuation / word-order variants
    ("User ID" <-> "user_id") but will NOT match genuine semantic
    equivalents like "Record Number" <-> "Report No." -- use the
    runner-level `field_mapping` for those.
    """
    na, nb = _normalize_for_matching(a), _normalize_for_matching(b)
    char_ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if ta or tb:
        jaccard = len(ta & tb) / len(ta | tb)
    else:
        jaccard = 1.0
    return max(char_ratio, jaccard)


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

    # Generic params every parser supports without redeclaring. Merged with
    # `PARSER_PARAMS` for typo-gate validation in `__init__`.
    _BASE_PARSER_PARAMS: ClassVar[tuple[str, ...]] = ("field_matching_policy",)

    # Each parser's default policy. Subclasses override to match their format's
    # natural semantic: CSV/Excel default to "positional" (header text is
    # potentially stale); JSON defaults to "exact" (the parser already does
    # `code -> name` translation that produces authoritatively-named columns).
    default_field_matching_policy: ClassVar[str] = "positional"

    params: dict[str, Any]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        raw = dict(params or {})
        type(self).validate_params(raw, ctx=f"parser {self.name!r}")
        self.params = raw

    # ------------------------------------------------------------------
    # Param-allowlist gate (one canonical home; called from __init__ and
    # from every YAML loader that builds a params dict for a parser).
    # ------------------------------------------------------------------

    @classmethod
    def accepted_params(cls) -> frozenset[str]:
        """Union of the parser's declared params plus the base-class generic
        params (field_matching_policy). Used by the typo gate."""
        return frozenset(cls.PARSER_PARAMS) | frozenset(cls._BASE_PARSER_PARAMS)

    @classmethod
    def validate_params(cls, params: dict[str, Any], *, ctx: str) -> None:
        """Raise `ConfigError` if `params` carries unknown keys or an invalid
        `field_matching_policy`. `ctx` prefixes the error so the caller controls
        whether it reads `"parser 'csv': ..."`, `"<yaml>: 'csv' ..."`, or
        `"<yaml>: tables.<T> ..."`.
        """
        accepted = cls.accepted_params()
        unknown = sorted(set(params) - accepted)
        if unknown:
            raise ConfigError(
                f"{ctx}: unknown keys {unknown}; "
                f"accepted: {sorted(accepted)}"
            )
        if "field_matching_policy" in params:
            policy = params["field_matching_policy"]
            if policy not in _VALID_FIELD_MATCHING_POLICIES:
                raise ConfigError(
                    f"{ctx}: field_matching_policy={policy!r} must be "
                    f"one of {sorted(_VALID_FIELD_MATCHING_POLICIES)}"
                )

    # ------------------------------------------------------------------
    # Plugin entry point
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_file(
        self, path: Path, *, table_name_hint: str | None = None,
    ) -> ParsedFile:
        """Read ONE source file. Return a `ParsedFile` carrying rows and
        an optional `ParserSchema`. The framework's `read()` handles
        everything else (tracking columns, multi-file concat, pl.String
        coercion). What "rows" and "column names" mean inside a `ParsedFile`
        is owned by the concrete parser.

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
        self,
        paths: list[Path],
        *,
        table_name_hint: str | None = None,
        contract_fields: list[tuple[str, str | None]] | None = None,
        similarity_threshold: float = 0.8,
    ) -> ReadResult:
        """Read all `paths` and return a unified LazyFrame plus the
        per-file schemas.

        Bookkeeping handled here so plugin authors never have to:
          - Every data column forced to `pl.String`.
          - `__source_file__` (filename) and `__row_index__` (1-based per
            file) added as tracking columns.
          - Multi-file frames concatenated via `diagonal_relaxed` so
            differing columns across files become null in the union.

        `contract_fields` is a list of `(name, source_name)` pairs. `name` is
        the contract field's database identifier (what columns get renamed
        to); `source_name` is the verbatim spec header (e.g. "Reference
        Number") used by `exact` and `similarity` policies to match raw
        CSV/Excel/JSON headers before falling back to `name`. When omitted,
        the rename step is a no-op -- handy for standalone parser use.
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
            # Apply the field-matching policy per file, BEFORE tracking
            # columns + concat so each file's columns bind to contract
            # field names independently. Multi-file reads with disagreeing
            # headers (CSV) or disagreeing `report_header` translations
            # (JSON) still unify cleanly after concat-by-name.
            lf = self._apply_field_matching(
                lf, path,
                contract_fields=contract_fields,
                similarity_threshold=similarity_threshold,
            )
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

    # ------------------------------------------------------------------
    # Field-matching policy
    # ------------------------------------------------------------------

    def _apply_field_matching(
        self,
        lf: Any,
        path: Path,
        *,
        contract_fields: list[tuple[str, str | None]] | None,
        similarity_threshold: float,
    ) -> Any:
        """Rename the per-file frame's columns to contract field `name`s per
        the configured policy. No-op when `contract_fields` is unset (parser
        used standalone) or when the chosen policy produces no rename.

        Per-file by construction: called inside `read()`'s loop before the
        multi-file concat, so two files whose parser-emitted column names
        disagree still concat cleanly under contract field names.

        `exact` and `similarity` policies match against `source_name` first
        (when set), then fall back to `name`. This lets the contract carry
        business-friendly headers ("Reference Number") and still bind raw
        CSVs/Excels that use those headers.
        """
        if not contract_fields:
            return lf
        existing = lf.collect_schema().names()
        names = [n for n, _ in contract_fields]
        policy = self.params.get("field_matching_policy", self.default_field_matching_policy)
        if policy == "positional":
            rename_map = self._positional_match_map(existing, names, path)
        elif policy == "exact":
            rename_map = self._exact_match_map(existing, contract_fields)
        elif policy == "similarity":
            rename_map = self._similarity_match_map(
                existing, contract_fields, similarity_threshold,
            )
        else:
            # Already validated in __init__; defensive.
            raise ConfigError(
                f"parser {self.name!r}: unknown field_matching_policy {policy!r}"
            )
        if not rename_map:
            return lf
        return lf.rename(rename_map)

    def _positional_match_map(
        self, existing: list[str], contract_field_names: list[str], path: Path,
    ) -> dict[str, str]:
        """Build a `{existing_name: contract_name}` map for positional mode.

        The i-th existing data column becomes the i-th contract field. Only
        the first `min(N_existing, N_contract)` columns are renamed; any
        extras on either side are surfaced by `column_missing` / `extra_column`
        downstream. No-op renames (existing already equals contract) are
        skipped.

        Raises `ConfigError` when a contract name to be assigned at position i
        collides with an existing column at position j >= len(contract). Polars
        would silently produce duplicates otherwise; this protects the user
        from a silent value scramble.
        """
        n = min(len(existing), len(contract_field_names))
        rename_map = {
            existing[i]: contract_field_names[i]
            for i in range(n)
            if existing[i] != contract_field_names[i]
        }
        duplicate_targets = sorted(
            set(contract_field_names[:n]) & set(existing[n:])
        )
        if duplicate_targets:
            raise ConfigError(
                f"parser {self.name!r}: positional rename in {path.name} would "
                f"create duplicate columns {duplicate_targets}. The file has "
                f"columns with these names AT DIFFERENT POSITIONS than the "
                f"contract declares. Either reorder the file, switch to "
                f"`field_matching_policy: exact` for this table, or rename "
                f"the colliding contract fields."
            )
        return rename_map

    def _exact_match_map(
        self,
        existing: list[str],
        contract_fields: list[tuple[str, str | None]],
    ) -> dict[str, str]:
        """Build a `{existing_name: contract_name}` map for exact mode.

        For each existing column: rename to a contract field's `name` if the
        column text matches the contract's `source_name` exactly, OR if it
        already equals the `name`. `source_name` is tried first so business-
        friendly headers ("Reference Number") win over a name collision.
        Columns that match neither are left alone -- downstream
        `column_missing` / `extra_column` surface them.
        """
        rename_map: dict[str, str] = {}
        used_names: set[str] = set()
        # Build lookups: source_name first (higher priority), then name as fallback.
        by_source: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for name, source_name in contract_fields:
            if source_name:
                by_source.setdefault(source_name, name)
            by_name.setdefault(name, name)
        for col in existing:
            target = by_source.get(col) or by_name.get(col)
            if target is None or target in used_names:
                continue
            if col != target:
                rename_map[col] = target
            used_names.add(target)
        return rename_map

    def _similarity_match_map(
        self,
        existing: list[str],
        contract_fields: list[tuple[str, str | None]],
        threshold: float,
    ) -> dict[str, str]:
        """Build a `{existing_name: contract_name}` map for similarity mode.

        Greedy assignment: iterate existing columns in order, find each one's
        best-scoring contract field above `threshold`, claim it exclusively.
        Exact matches against `source_name` (when set) or `name` short-circuit
        (no scoring needed). Mismatches that can't clear the threshold stay
        as-is; downstream `column_missing` / `extra_column` surface them.
        """
        # Pair each contract entry with the candidate string used for matching:
        # source_name when set (business-friendly), else name (already a slug).
        candidates: list[tuple[str, str]] = [
            (name, source_name or name) for name, source_name in contract_fields
        ]
        used: set[str] = set()
        rename_map: dict[str, str] = {}
        for col in existing:
            # Exact-match shortcut against either source_name or name.
            exact = next(
                (name for name, cand in candidates if name not in used and (col == cand or col == name)),
                None,
            )
            if exact is not None:
                used.add(exact)
                continue
            best: str | None = None
            best_score = threshold
            for name, cand in candidates:
                if name in used:
                    continue
                score = _similarity_score(col, cand)
                if score >= best_score:
                    best_score = score
                    best = name
            if best is not None:
                rename_map[col] = best
                used.add(best)
        return rename_map

    # ------------------------------------------------------------------
    # Bookkeeping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_tracking(lf: Any, path: Path, pl_module) -> Any:
        """Add `__row_index__` (1-based per file) and `__source_file__`
        (filename)."""
        return lf.with_row_index(name="__row_index__", offset=1).with_columns(
            pl_module.lit(path.name).alias("__source_file__"),
        )
