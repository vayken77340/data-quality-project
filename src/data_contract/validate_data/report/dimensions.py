"""Quality-dimension taxonomy + score calculation.

Single source of truth for the four data-quality dimensions (DAMA-DMBOK /
ISO 8000) and the rules that map violation `kind` strings to dimensions.
Also owns the score formula used by every report format.

Operational kinds (`no_input_files`, `parser_failure`,
`fk_target_table_not_loaded`) are NOT data-quality dimensions -- they're
environment failures and are surfaced separately as "Run issues" in each
format. They participate in `Dimension.OPERATIONAL` for routing but do
not contribute to the quality score (an operational failure short-circuits
the per-table score to 0 instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Dimension(str, Enum):
    COMPLETENESS = "completeness"
    VALIDITY     = "validity"
    UNIQUENESS   = "uniqueness"
    CONSISTENCY  = "consistency"
    OPERATIONAL  = "operational"     # not a quality dimension; bucket for env failures


# Industry-standard four (DAMA-DMBOK) + an operational bucket. Every existing
# violation kind in the codebase maps to exactly one dimension; adding a new
# kind without listing it here raises KeyError at first use -- the typo gate.
_KIND_TO_DIMENSION: dict[str, Dimension] = {
    # Completeness
    "nullable_violation":          Dimension.COMPLETENESS,
    "column_missing":              Dimension.COMPLETENESS,
    # Validity
    "type_coercion_violation":     Dimension.VALIDITY,
    "max_length_violation":        Dimension.VALIDITY,
    "boolean_coercion_violation":  Dimension.VALIDITY,
    "pattern_violation":           Dimension.VALIDITY,
    "format_violation":            Dimension.VALIDITY,
    "allowed_values_violation":    Dimension.VALIDITY,
    "min_value_violation":         Dimension.VALIDITY,
    "max_value_violation":         Dimension.VALIDITY,
    "extra_column":                Dimension.VALIDITY,
    # Uniqueness
    "pk_not_unique":               Dimension.UNIQUENESS,
    "unique_violation":            Dimension.UNIQUENESS,
    # Consistency
    "fk_not_found":                Dimension.CONSISTENCY,
    # Operational (environment failures; never count towards quality score)
    "no_input_files":              Dimension.OPERATIONAL,
    "parser_failure":              Dimension.OPERATIONAL,
    "fk_target_table_not_loaded":  Dimension.OPERATIONAL,
}


# The four QUALITY dimensions (excludes OPERATIONAL). Use this for any UI
# that displays "data quality dimensions" -- operational issues live elsewhere.
QUALITY_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.COMPLETENESS,
    Dimension.VALIDITY,
    Dimension.UNIQUENESS,
    Dimension.CONSISTENCY,
)


# Maps the `checks:` block names (knobs in validation.yaml) to the violation
# kind(s) they emit when triggered. The report's Checks section surfaces this
# pairing so authors can trace each knob to the failure kind it controls.
# Per-constraint checks (allowed_values, pattern, min/max_value, format, unique)
# match the constraint registry's `cls.name` -> `cls.VIOLATION_KIND` mapping.
_CHECK_TO_VIOLATION_KIND: dict[str, str] = {
    # Core
    "type_coercion":    "type_coercion_violation",
    "boolean_coercion": "boolean_coercion_violation",
    "nullable":         "nullable_violation",
    "max_length":       "max_length_violation",
    "column_missing":   "column_missing",
    # Keys
    "pk_uniqueness":    "pk_not_unique",
    "fk_existence":     "fk_not_found",
    # Per-constraint (must stay in lockstep with FieldConstraint subclasses)
    "allowed_values":   "allowed_values_violation",
    "pattern":          "pattern_violation",
    "min_value":        "min_value_violation",
    "max_value":        "max_value_violation",
    "format":           "format_violation",
    "unique":           "unique_violation",
}


def violation_kind_for_check(check_name: str) -> str:
    """Return the violation kind emitted by `check_name`. Raises KeyError on
    unknown -- adding a new check requires adding it here too."""
    try:
        return _CHECK_TO_VIOLATION_KIND[check_name]
    except KeyError:
        raise KeyError(
            f"unknown check name {check_name!r}; add it to "
            f"validate_data.report.dimensions._CHECK_TO_VIOLATION_KIND"
        ) from None


def dimension_for(kind: str) -> Dimension:
    """Return the dimension for a violation kind. Raises KeyError on unknown."""
    try:
        return _KIND_TO_DIMENSION[kind]
    except KeyError:
        raise KeyError(
            f"unknown violation kind {kind!r}; add it to "
            f"validate_data.report.dimensions._KIND_TO_DIMENSION "
            f"(and ensure it has a hint in hints.py)"
        ) from None


def kinds_in_dimension(dim: Dimension) -> set[str]:
    """Return the set of violation kinds tagged with `dim`."""
    return {k for k, d in _KIND_TO_DIMENSION.items() if d is dim}


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------


_ERROR_SEVERITY = "error"
_OPERATIONAL_KINDS_TANKING_SCORE = frozenset({"no_input_files", "parser_failure"})


@dataclass(frozen=True)
class DimensionScore:
    score: float                          # 0.0 - 100.0
    violations: int                       # count of error-severity violations in this dimension
    affected_rows: int                    # distinct rows with at least one violation in this dimension


@dataclass(frozen=True)
class TableScore:
    score: float                          # 0.0 - 100.0 (overall for this table)
    by_dimension: dict[Dimension, DimensionScore]
    affected_rows: int                    # distinct rows with any error-severity violation
    clean_rows: int                       # total_rows - affected_rows
    operational_failure: bool             # True if no_input_files / parser_failure present


def compute_table_score(
    *,
    total_rows: int,
    violations: list[Any],                # list[Violation]; declared Any to avoid runner import cycle
) -> TableScore:
    """Compute the per-table score plus per-dimension scores.

    Score formula: `(rows_clean / total_rows) * 100` where `rows_clean` is the
    count of distinct (source_file, source_row) tuples WITHOUT any error-
    severity violation. Rows with multiple error violations count ONCE (this
    is the fix for today's broken `total - n_violations / total` math which
    could go negative).

    Per-dimension score: same formula restricted to that dimension's kinds.

    Operational failure: if any violation has `no_input_files` or
    `parser_failure` kind, the table score collapses to 0.0 (we couldn't
    validate the data at all) -- per-dimension scores stay computed normally
    so the dimension panel still shows what we DID measure.
    """
    operational_failure = any(
        v.kind in _OPERATIONAL_KINDS_TANKING_SCORE for v in violations
    )

    error_keys_overall: set[tuple] = set()
    error_keys_by_dim: dict[Dimension, set[tuple]] = {d: set() for d in QUALITY_DIMENSIONS}
    violations_by_dim: dict[Dimension, int] = {d: 0 for d in QUALITY_DIMENSIONS}

    for v in violations:
        if v.severity != _ERROR_SEVERITY:
            continue
        dim = dimension_for(v.kind)
        if dim is Dimension.OPERATIONAL:
            continue   # operational failures bypass the per-dimension score
        violations_by_dim[dim] += 1
        if v.source_file is not None and v.source_row is not None:
            key = (v.source_file, v.source_row)
            error_keys_overall.add(key)
            error_keys_by_dim[dim].add(key)

    affected_rows = len(error_keys_overall)
    clean_rows = max(total_rows - affected_rows, 0)

    if operational_failure or total_rows == 0:
        table_score = 0.0
    else:
        table_score = round((clean_rows / total_rows) * 100, 2)

    by_dim: dict[Dimension, DimensionScore] = {}
    for d in QUALITY_DIMENSIONS:
        affected = len(error_keys_by_dim[d])
        if total_rows == 0:
            dim_score = 0.0 if violations_by_dim[d] else 100.0
        else:
            dim_score = round(((total_rows - affected) / total_rows) * 100, 2)
        by_dim[d] = DimensionScore(
            score=dim_score,
            violations=violations_by_dim[d],
            affected_rows=affected,
        )

    return TableScore(
        score=table_score,
        by_dimension=by_dim,
        affected_rows=affected_rows,
        clean_rows=clean_rows,
        operational_failure=operational_failure,
    )


@dataclass(frozen=True)
class OverallScore:
    score: float
    by_dimension: dict[Dimension, DimensionScore]


def compute_overall_score(
    table_scores: list[tuple[int, TableScore]],
) -> OverallScore:
    """Aggregate per-table scores into an overall score.

    Overall = row-count-weighted average of per-table scores. Empty tables
    (total_rows == 0) are excluded from the weighting. Per-dimension overall
    is the same weighted average per-dimension.
    """
    weighted_sum = 0.0
    total_weight = 0
    per_dim_weighted: dict[Dimension, float] = {d: 0.0 for d in QUALITY_DIMENSIONS}
    per_dim_violations: dict[Dimension, int] = {d: 0 for d in QUALITY_DIMENSIONS}
    per_dim_affected: dict[Dimension, int] = {d: 0 for d in QUALITY_DIMENSIONS}

    for rows, ts in table_scores:
        if rows == 0:
            continue
        weighted_sum += ts.score * rows
        total_weight += rows
        for d in QUALITY_DIMENSIONS:
            per_dim_weighted[d] += ts.by_dimension[d].score * rows
            per_dim_violations[d] += ts.by_dimension[d].violations
            per_dim_affected[d]   += ts.by_dimension[d].affected_rows

    overall = round(weighted_sum / total_weight, 2) if total_weight else 0.0
    by_dim: dict[Dimension, DimensionScore] = {
        d: DimensionScore(
            score=round(per_dim_weighted[d] / total_weight, 2) if total_weight else 0.0,
            violations=per_dim_violations[d],
            affected_rows=per_dim_affected[d],
        )
        for d in QUALITY_DIMENSIONS
    }
    return OverallScore(score=overall, by_dimension=by_dim)
