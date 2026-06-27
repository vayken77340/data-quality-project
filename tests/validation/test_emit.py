"""Direct tests for `validation.emit.emit_from_lazy` and `extract_pk_values`."""

from __future__ import annotations

import polars as pl
import pytest

from data_contract.contract import FieldContract
from data_contract.type_mapping import Type
from data_contract.validation.emit import emit_from_lazy, extract_pk_values
from data_contract.validation.models import TableReport
from tests.conftest import field_contract as _field


def _report() -> TableReport:
    return TableReport(
        table="T", contract_version="1.0", pk_fields=["id"], input_files=[],
    )


def test_emit_from_lazy_noop_on_none():
    report = _report()
    emit_from_lazy(
        None, kind="x", severity="error", table="T",
        field=_field("a"), pk_cols=["id"], expected="", report=report,
    )
    assert report.violations == []


def test_emit_from_lazy_emits_one_per_row():
    """Every row in the LazyFrame becomes one Violation on the report."""
    lf = pl.DataFrame({
        "a": ["x", "y"],
        "id": [1, 2],
        "__source_file__": ["f.csv", "f.csv"],
        "__row_index__": [3, 7],
    }).lazy()
    report = _report()
    emit_from_lazy(
        lf, kind="bad", severity="error", table="T",
        field=_field("a"), pk_cols=["id"], expected="must be valid",
        report=report,
    )
    assert len(report.violations) == 2
    v0 = report.violations[0]
    assert v0.kind == "bad"
    assert v0.field == "a"
    assert v0.source_file == "f.csv"
    assert v0.source_row == 3
    assert v0.pk_values == {"id": 1}
    assert v0.offending_value == "x"
    assert v0.expected == "must be valid"


def test_emit_from_lazy_field_none_yields_no_offending_value():
    """Table-level emissions (no specific field) don't claim an offending column."""
    lf = pl.DataFrame({
        "id": [1],
        "__source_file__": ["f.csv"],
        "__row_index__": [9],
    }).lazy()
    report = _report()
    emit_from_lazy(
        lf, kind="table_kind", severity="warning", table="T",
        field=None, pk_cols=["id"], expected="", report=report,
    )
    assert len(report.violations) == 1
    assert report.violations[0].field is None
    assert report.violations[0].offending_value is None


def test_emit_from_lazy_no_source_columns_yields_none_metadata():
    """Frames without tracking columns still emit; source_file / source_row are None."""
    lf = pl.DataFrame({"a": ["x"]}).lazy()
    report = _report()
    emit_from_lazy(
        lf, kind="k", severity="error", table="T",
        field=_field("a"), pk_cols=[], expected="", report=report,
    )
    assert len(report.violations) == 1
    assert report.violations[0].source_file is None
    assert report.violations[0].source_row is None


def test_extract_pk_values_empty_pk_returns_none():
    """Tables with no PK columns get None (not {}) for pk_values."""
    assert extract_pk_values({"id": 1}, []) is None


def test_extract_pk_values_picks_only_pk_cols():
    row = {"id": 1, "name": "x", "extra": 2}
    assert extract_pk_values(row, ["id", "name"]) == {"id": 1, "name": "x"}


def test_extract_pk_values_missing_column_yields_none():
    """A PK column that isn't present in the row dict yields None for that key
    (not a KeyError)."""
    assert extract_pk_values({"id": 1}, ["id", "missing"]) == {"id": 1, "missing": None}
