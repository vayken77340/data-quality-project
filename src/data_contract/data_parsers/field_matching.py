"""Field-matching policies for `FileParser.read()`.

Three policies plus a couple of helpers. Pure functions over
`(existing_columns, contract_fields, threshold)` — no parser instance
state. `FileParser.read()` calls `apply_policy(...)` once per file with
the parser's resolved policy; `validate_params` in `FileParser` calls
`validate_policy(...)` to typo-gate `field_matching_policy` values from
YAML.

The three policies:

  * `positional` (CSV / Excel default) — the i-th data column is the
    i-th contract field. Header text is ignored.
  * `exact` (JSON default) — columns whose names match a contract
    field's `source_name` (preferred) or `name` bind by string equality.
  * `similarity` — fuzzy match against `source_name` (preferred) /
    `name` with a caller-supplied threshold.

Each policy returns a `{existing_name: contract_name}` rename map.
`apply_policy` applies the map to a LazyFrame.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from dq_core.errors import ConfigError


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


def validate_policy(policy: Any, *, ctx: str) -> None:
    """Raise `ConfigError` if `policy` is not one of the recognised values.
    `ctx` prefixes the error so the caller controls whether it reads
    `"parser 'csv': ..."`, `"<yaml>: 'csv' ..."`, etc."""
    if policy not in _VALID_FIELD_MATCHING_POLICIES:
        raise ConfigError(
            f"{ctx}: field_matching_policy={policy!r} must be "
            f"one of {sorted(_VALID_FIELD_MATCHING_POLICIES)}"
        )


def apply_policy(
    lf: Any,
    path: Path,
    *,
    policy: str,
    contract_fields: list[tuple[str, str | None]] | None,
    similarity_threshold: float,
    parser_name: str,
) -> Any:
    """Rename the per-file frame's columns to contract field `name`s per
    `policy`. No-op when `contract_fields` is unset (parser used standalone)
    or when the chosen policy produces no rename.

    Per-file by construction: callers invoke this inside the multi-file
    loop, before the diagonal concat, so two files whose parser-emitted
    column names disagree still concat cleanly under contract field names.

    `exact` and `similarity` policies match against `source_name` first
    (when set), then fall back to `name`. This lets the contract carry
    business-friendly headers ("Reference Number") and still bind raw
    CSVs/Excels that use those headers.
    """
    if not contract_fields:
        return lf
    existing = lf.collect_schema().names()
    names = [n for n, _ in contract_fields]
    if policy == "positional":
        rename_map = positional_match_map(existing, names, path, parser_name=parser_name)
    elif policy == "exact":
        rename_map = exact_match_map(existing, contract_fields)
    elif policy == "similarity":
        rename_map = similarity_match_map(existing, contract_fields, similarity_threshold)
    else:
        # Already validated by `validate_policy`; defensive.
        raise ConfigError(
            f"parser {parser_name!r}: unknown field_matching_policy {policy!r}"
        )
    if not rename_map:
        return lf
    return lf.rename(rename_map)


def positional_match_map(
    existing: list[str],
    contract_field_names: list[str],
    path: Path,
    *,
    parser_name: str,
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
            f"parser {parser_name!r}: positional rename in {path.name} would "
            f"create duplicate columns {duplicate_targets}. The file has "
            f"columns with these names AT DIFFERENT POSITIONS than the "
            f"contract declares. Either reorder the file, switch to "
            f"`field_matching_policy: exact` for this table, or rename "
            f"the colliding contract fields."
        )
    return rename_map


def exact_match_map(
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


def similarity_match_map(
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
