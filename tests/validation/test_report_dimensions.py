"""Tests for the dimension taxonomy + score calculation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dq_core.report.dimensions import (
    Dimension,
    QUALITY_DIMENSIONS,
    compute_overall_score,
    compute_table_score,
    dimension_for,
    kinds_in_dimension,
)


@dataclass
class _V:
    """Minimal Violation stand-in for score-calc tests."""
    kind: str
    severity: str = "error"
    source_file: str | None = "f.csv"
    source_row: int | None = 1


# ---------------------------------------------------------------------------
# Dimension mapping
# ---------------------------------------------------------------------------


def test_quality_dimensions_excludes_operational():
    assert Dimension.OPERATIONAL not in QUALITY_DIMENSIONS
    assert len(QUALITY_DIMENSIONS) == 4


def test_dimension_for_known_kinds():
    assert dimension_for("nullable_violation") is Dimension.COMPLETENESS
    assert dimension_for("type_coercion_violation") is Dimension.VALIDITY
    assert dimension_for("pk_not_unique") is Dimension.UNIQUENESS
    assert dimension_for("fk_not_found") is Dimension.CONSISTENCY
    assert dimension_for("no_input_files") is Dimension.OPERATIONAL


def test_dimension_for_unknown_raises():
    with pytest.raises(KeyError, match="unknown violation kind"):
        dimension_for("brand_new_kind_nobody_added")


def test_every_existing_kind_has_a_dimension():
    """Guard: every kind emitted by the runner must be mapped here."""
    from dq_core.field_constraints import REGISTRY
    # Per-constraint kinds (from each registered FieldConstraint).
    for cls in REGISTRY.values():
        vk = getattr(cls, "VIOLATION_KIND", None)
        if vk:
            dimension_for(vk)   # raises if missing
    # Core hard-coded kinds emitted by the runner.
    for k in (
        "nullable_violation", "max_length_violation", "type_coercion_violation",
        "boolean_coercion_violation", "column_missing", "extra_column",
        "pk_not_unique", "fk_not_found", "fk_target_table_not_loaded",
        "no_input_files", "parser_failure",
    ):
        dimension_for(k)


def test_kinds_in_dimension():
    completeness = kinds_in_dimension(Dimension.COMPLETENESS)
    assert "nullable_violation" in completeness
    assert "column_missing" in completeness
    assert "type_coercion_violation" not in completeness


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------


def test_compute_table_score_clean():
    ts = compute_table_score(total_rows=5, violations=[])
    assert ts.score == 100.0
    assert ts.affected_rows == 0
    assert ts.clean_rows == 5
    assert ts.operational_failure is False
    for d in QUALITY_DIMENSIONS:
        assert ts.by_dimension[d].score == 100.0
        assert ts.by_dimension[d].violations == 0


def test_compute_table_score_one_bad_row():
    v = _V("nullable_violation", source_file="a.csv", source_row=2)
    ts = compute_table_score(total_rows=5, violations=[v])
    # 4 of 5 rows clean.
    assert ts.score == 80.0
    assert ts.affected_rows == 1
    assert ts.clean_rows == 4


def test_compute_table_score_multi_violation_row_counted_once():
    """The bug fix: row 2 has TWO error violations but should count as ONE
    affected row -- prevents the score from going below 0 or being misleading."""
    v1 = _V("nullable_violation", source_file="a.csv", source_row=2)
    v2 = _V("max_length_violation", source_file="a.csv", source_row=2)
    ts = compute_table_score(total_rows=5, violations=[v1, v2])
    assert ts.affected_rows == 1
    assert ts.score == 80.0
    # Per-dimension: both violations count separately for their bucket totals,
    # but affected_rows per dimension is still 1 each.
    assert ts.by_dimension[Dimension.COMPLETENESS].violations == 1
    assert ts.by_dimension[Dimension.VALIDITY].violations == 1
    assert ts.by_dimension[Dimension.COMPLETENESS].affected_rows == 1
    assert ts.by_dimension[Dimension.VALIDITY].affected_rows == 1


def test_compute_table_score_warning_does_not_lower_score():
    v = _V("max_length_violation", severity="warning")
    ts = compute_table_score(total_rows=5, violations=[v])
    # Only error severity participates in the score.
    assert ts.score == 100.0


def test_compute_table_score_operational_failure_short_circuits_to_zero():
    v = _V("no_input_files", source_file=None, source_row=None)
    ts = compute_table_score(total_rows=5, violations=[v])
    assert ts.score == 0.0
    assert ts.operational_failure is True


def test_compute_table_score_empty_table():
    ts = compute_table_score(total_rows=0, violations=[])
    assert ts.score == 0.0  # nothing to validate -> not a "passing" table
    for d in QUALITY_DIMENSIONS:
        assert ts.by_dimension[d].score == 100.0  # no violations in dimension


def test_per_dimension_score_restricts_to_dimension():
    # 1 completeness violation on row 1; 1 validity violation on row 2.
    v1 = _V("nullable_violation", source_file="a.csv", source_row=1)
    v2 = _V("max_length_violation", source_file="a.csv", source_row=2)
    ts = compute_table_score(total_rows=10, violations=[v1, v2])
    assert ts.affected_rows == 2
    assert ts.score == 80.0
    assert ts.by_dimension[Dimension.COMPLETENESS].score == 90.0   # 9/10 clean for completeness
    assert ts.by_dimension[Dimension.VALIDITY].score == 90.0       # 9/10 clean for validity
    assert ts.by_dimension[Dimension.UNIQUENESS].score == 100.0
    assert ts.by_dimension[Dimension.CONSISTENCY].score == 100.0


# ---------------------------------------------------------------------------
# Overall score (row-count-weighted)
# ---------------------------------------------------------------------------


def test_compute_overall_score_simple():
    ts1 = compute_table_score(total_rows=10, violations=[])
    ts2 = compute_table_score(total_rows=10, violations=[
        _V("nullable_violation", source_file="b.csv", source_row=1),
    ])
    overall = compute_overall_score([(10, ts1), (10, ts2)])
    # Average of 100 and 90 = 95.
    assert overall.score == 95.0


def test_compute_overall_score_weighted_by_rows():
    ts_big = compute_table_score(total_rows=900, violations=[])    # 100.0
    ts_small = compute_table_score(total_rows=10, violations=[
        _V("nullable_violation", source_file="b.csv", source_row=1),
    ])                                                              # 90.0
    overall = compute_overall_score([(900, ts_big), (10, ts_small)])
    # Weighted = (100*900 + 90*10) / 910 = (90000 + 900) / 910 = 99.89
    assert overall.score == 99.89


def test_compute_overall_score_skips_empty_tables():
    ts1 = compute_table_score(total_rows=10, violations=[])
    ts2 = compute_table_score(total_rows=0, violations=[])
    overall = compute_overall_score([(10, ts1), (0, ts2)])
    assert overall.score == 100.0
