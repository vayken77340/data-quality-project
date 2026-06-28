"""Quality-dimension taxonomy + score calculation.

Single source of truth for the four data-quality dimensions (DAMA-DMBOK /
ISO 8000) and the rules that map violation `kind` strings to dimensions.
Also owns the score formula used by every report format.

Three check tiers (the dev-extension story):

* **Structural** (`type_coercion`, `boolean_coercion`, `nullable`,
  `max_length`) -- derived from the contract's type system. Not pluggable
  by design: a "custom structural check" is really a new `Type` entry in
  `configs/types.yaml`. The check-name -> dimension mapping for this tier
  is hard-coded below.
* **Table-level** (`pk_uniqueness`, `fk_existence`, `column_missing`, ...)
  -- pluggable via the `dq_core.table_checks/` registry. Each
  `TableCheck` subclass declares its own `VIOLATION_KIND` and `DIMENSION`,
  which are folded into the maps below at import time.
* **Per-constraint** (`allowed_values`, `pattern`, `unique`, ...) --
  pluggable via the `dq_core.field_constraints/` registry. Each
  `FieldConstraint` subclass declares its own `VIOLATION_KIND`; the
  dimension is derived from a per-class `DIMENSION` ClassVar (defaulting
  to `validity` when unset, matching the DAMA bucket most constraints
  belong in).

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


# Structural-tier mappings (hard-coded; structural checks ARE the type system).
_STRUCTURAL_KIND_TO_DIMENSION: dict[str, Dimension] = {
    "nullable_violation":          Dimension.COMPLETENESS,
    "type_coercion_violation":     Dimension.VALIDITY,
    "max_length_violation":        Dimension.VALIDITY,
    "boolean_coercion_violation":  Dimension.VALIDITY,
    # Source-schema drift (default off; one source -> one warning kind).
    "field_name_drift":        Dimension.VALIDITY,
    "field_type_drift":        Dimension.VALIDITY,
    "field_type_unknown":         Dimension.VALIDITY,
}

_STRUCTURAL_CHECK_TO_KIND: dict[str, str] = {
    "type_coercion":        "type_coercion_violation",
    "boolean_coercion":     "boolean_coercion_violation",
    "nullable":             "nullable_violation",
    "max_length":           "max_length_violation",
    "field_names_from_sample":  "field_name_drift",
    "field_types_from_sample":  "field_type_drift",
}

# Operational kinds: bypass the quality-score machinery.
_OPERATIONAL_KIND_TO_DIMENSION: dict[str, Dimension] = {
    "no_input_files":              Dimension.OPERATIONAL,
    "parser_failure":              Dimension.OPERATIONAL,
    "fk_target_table_not_loaded":  Dimension.OPERATIONAL,
}

# Extra column is structural in nature (data file has columns the contract
# doesn't declare). Hard-coded because it isn't emitted by any registry check.
_EXTRA_KIND_TO_DIMENSION: dict[str, Dimension] = {
    "extra_column": Dimension.VALIDITY,
}


def _normalise_dimension(value: str) -> Dimension:
    try:
        return Dimension(value)
    except ValueError:
        raise KeyError(
            f"unknown DIMENSION {value!r}; expected one of "
            f"{[d.value for d in Dimension]}"
        ) from None


def _build_check_to_kind_and_kind_to_dim() -> tuple[dict[str, str], dict[str, Dimension]]:
    """Fold the structural maps + the two registries into the two flat maps
    every report consumer reads. Re-computed at import-time only -- if a
    test registers a new plugin mid-process it must call
    `refresh_dimension_maps()` below.
    """
    from dq_core import field_constraints as fc_pkg
    from dq_core import table_checks as tc_pkg

    check_to_kind: dict[str, str] = dict(_STRUCTURAL_CHECK_TO_KIND)
    kind_to_dim: dict[str, Dimension] = {}
    kind_to_dim.update(_STRUCTURAL_KIND_TO_DIMENSION)
    kind_to_dim.update(_OPERATIONAL_KIND_TO_DIMENSION)
    kind_to_dim.update(_EXTRA_KIND_TO_DIMENSION)

    for name, cls in tc_pkg.REGISTRY.items():
        check_to_kind[name] = cls.VIOLATION_KIND
        kind_to_dim[cls.VIOLATION_KIND] = _normalise_dimension(cls.DIMENSION)

    for name, cls in fc_pkg.REGISTRY.items():
        # Skip contract-only constraints (no data-side check, e.g. default_value).
        if not isinstance(cls.VIOLATION_KIND, str) or not cls.VIOLATION_KIND:
            continue
        check_to_kind[name] = cls.VIOLATION_KIND
        # FieldConstraint subclasses may declare DIMENSION; default to validity
        # for the common case (range / pattern / format checks are validity rules).
        dim_raw = getattr(cls, "DIMENSION", None)
        if dim_raw:
            kind_to_dim[cls.VIOLATION_KIND] = _normalise_dimension(dim_raw)
        else:
            # `unique_violation` is uniqueness; everything else without an explicit
            # declaration falls under validity by default.
            if cls.VIOLATION_KIND == "unique_violation":
                kind_to_dim[cls.VIOLATION_KIND] = Dimension.UNIQUENESS
            else:
                kind_to_dim[cls.VIOLATION_KIND] = Dimension.VALIDITY

    return check_to_kind, kind_to_dim


_CHECK_TO_VIOLATION_KIND, _KIND_TO_DIMENSION = _build_check_to_kind_and_kind_to_dim()


def refresh_dimension_maps() -> None:
    """Rebuild the module-level maps after a runtime plugin registration.

    Tests that register a custom TableCheck / FieldConstraint mid-process
    must call this; production loads import-once and never need it.
    """
    global _CHECK_TO_VIOLATION_KIND, _KIND_TO_DIMENSION
    _CHECK_TO_VIOLATION_KIND, _KIND_TO_DIMENSION = _build_check_to_kind_and_kind_to_dim()


# The four QUALITY dimensions (excludes OPERATIONAL). Use this for any UI
# that displays "data quality dimensions" -- operational issues live elsewhere.
QUALITY_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.COMPLETENESS,
    Dimension.VALIDITY,
    Dimension.UNIQUENESS,
    Dimension.CONSISTENCY,
)


def violation_kind_for_check(check_name: str) -> str:
    """Return the violation kind emitted by `check_name`. Raises KeyError on
    unknown -- adding a new check requires registering it in the proper
    registry (table_checks/ or field_constraints/) or extending the
    hard-coded structural map above."""
    try:
        return _CHECK_TO_VIOLATION_KIND[check_name]
    except KeyError:
        raise KeyError(
            f"unknown check name {check_name!r}; register it in "
            f"dq_core.table_checks or dq_core.field_constraints, "
            f"or (for structural type-system checks) add it to "
            f"_STRUCTURAL_CHECK_TO_KIND in dimensions.py"
        ) from None


def dimension_for(kind: str) -> Dimension:
    """Return the dimension for a violation kind. Raises KeyError on unknown."""
    try:
        return _KIND_TO_DIMENSION[kind]
    except KeyError:
        raise KeyError(
            f"unknown violation kind {kind!r}; ensure the emitting check "
            f"declares a DIMENSION ClassVar (table_checks/field_constraints) "
            f"or that the kind is mapped in the structural section of "
            f"dimensions.py (and that it has a hint in hints.py)"
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
    severity violation. Rows with multiple error violations count ONCE.

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
