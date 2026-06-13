"""Tests for the per-(table, check) status aggregator."""

from __future__ import annotations

from data_contract.validation.report.check_status import (
    STATUS_ERROR,
    STATUS_NA,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WARNING,
    compute_table_check_status,
)
from data_contract.violations import Violation


_CHECK_TO_KIND = {
    "nullable": "nullable_violation",
    "pk_uniqueness": "pk_not_unique",
    "column_missing": "column_missing",
}


def _v(kind: str, severity: str, *, source_file: str | None = "a.csv",
       source_row: int | None = 1, field: str | None = None) -> Violation:
    return Violation(
        kind=kind, severity=severity, table="T", field=field,
        source_file=source_file, source_row=source_row,
        expected="x",
    )


def test_all_pass_row_level_check():
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=100, violations=[], enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    assert statuses["nullable"].status == STATUS_OK
    assert statuses["nullable"].pass_rows == 100
    assert statuses["nullable"].error_rows == 0
    assert statuses["nullable"].warning_rows == 0


def test_row_level_check_with_errors_and_warnings_on_disjoint_rows():
    violations = [
        _v("nullable_violation", "error", source_file="a.csv", source_row=1),
        _v("nullable_violation", "warning", source_file="a.csv", source_row=2),
    ]
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=10, violations=violations, enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    s = statuses["nullable"]
    assert s.status == STATUS_ERROR
    assert s.error_rows == 1
    assert s.warning_rows == 1
    assert s.pass_rows == 8


def test_row_level_check_same_row_two_violations_counted_once():
    """A row violating the same check twice counts as one affected row."""
    violations = [
        _v("nullable_violation", "error", source_row=1, field="a"),
        _v("nullable_violation", "error", source_row=1, field="b"),
    ]
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=10, violations=violations, enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    assert statuses["nullable"].error_rows == 1
    assert statuses["nullable"].pass_rows == 9


def test_disabled_check_skipped_with_zero_counts():
    enabled = {n: True for n in _CHECK_TO_KIND}
    enabled["pk_uniqueness"] = False
    statuses = compute_table_check_status(
        total_rows=10, violations=[], enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    s = statuses["pk_uniqueness"]
    assert s.status == STATUS_SKIPPED
    assert s.error_rows == 0 and s.warning_rows == 0
    assert s.pass_rows is None


def test_table_level_kind_counts_violations_not_rows():
    violations = [
        _v("column_missing", "error", source_file=None, source_row=None, field="a"),
        _v("column_missing", "error", source_file=None, source_row=None, field="b"),
    ]
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=10, violations=violations, enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    s = statuses["column_missing"]
    assert s.scope == "table"
    assert s.status == STATUS_ERROR
    assert s.violation_count == 2
    assert s.pass_rows is None


def test_operational_short_circuit_makes_row_checks_na():
    """no_input_files / parser_failure mean we couldn't run the row-level
    checks at all -- their grid cells should be NA, not OK."""
    violations = [
        _v("no_input_files", "error", source_file=None, source_row=None),
    ]
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=0, violations=violations, enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    assert statuses["nullable"].status == STATUS_NA
    assert statuses["pk_uniqueness"].status == STATUS_NA


def test_warning_only_row_check_status_is_warning():
    violations = [
        _v("nullable_violation", "warning", source_row=1),
    ]
    enabled = {n: True for n in _CHECK_TO_KIND}
    statuses = compute_table_check_status(
        total_rows=10, violations=violations, enabled_checks=enabled,
        check_to_kind=_CHECK_TO_KIND,
    )
    assert statuses["nullable"].status == STATUS_WARNING
    assert statuses["nullable"].warning_rows == 1
    assert statuses["nullable"].pass_rows == 9
