"""Tests for the built-in TableMetric implementations."""

from __future__ import annotations

import polars as pl

from dq_core.contract import Contract
from dq_core.metrics.completeness import CompletenessMetric
from dq_core.metrics.distinct_count import DistinctCountMetric
from dq_core.metrics.duplicate_pct import DuplicatePctMetric
from dq_core.metrics.null_count import NullCountMetric
from dq_core.metrics.null_percentage import NullPercentageMetric
from dq_core.metrics.row_count import RowCountMetric


def _fixture():
    """A small frame with known null + distinct counts.

    a: 4 rows total, 1 null, 3 non-null distinct values (1,2,3).
    b: 4 rows total, 0 nulls, 2 distinct non-null values (x repeated 3x, y once)
       -> duplicate_pct should be (3 - 2) / 4 * 100 = 25.0 (3 dup rows of x,
       counting all-but-one as duplicates: actually the metric formula is
       (non_null - distinct_non_null) / total * 100 = (4 - 2)/4*100 = 50.0).
    """
    df = pl.DataFrame({
        "a": [1, 2, None, 3],
        "b": ["x", "x", "x", "y"],
    })
    contract = Contract.from_dict({
        "version": "1.0",
        "epic": "T",
        "table": "T",
        "spec": {"file_path": "s", "sheet_name": "T"},
        "fields": [
            {"name": "a", "type": "int32", "nullable": True},
            {"name": "b", "type": "string", "nullable": False, "max_length": 10},
        ],
    })
    return df, contract


def test_null_count_per_field():
    df, contract = _fixture()
    res = NullCountMetric().compute(df, contract, None)
    assert res.scope == "field"
    assert res.values == {"a": 1, "b": 0}


def test_null_percentage_per_field():
    df, contract = _fixture()
    res = NullPercentageMetric().compute(df, contract, None)
    assert res.values["a"] == 25.0
    assert res.values["b"] == 0.0


def test_distinct_count_excludes_nulls():
    df, contract = _fixture()
    res = DistinctCountMetric().compute(df, contract, None)
    # a has non-null values 1, 2, 3 -> 3 distinct. b has x, y -> 2 distinct.
    assert res.values == {"a": 3, "b": 2}


def test_completeness_is_inverse_of_null_percentage():
    df, contract = _fixture()
    res = CompletenessMetric().compute(df, contract, None)
    assert res.values["a"] == 75.0
    assert res.values["b"] == 100.0


def test_duplicate_pct_counts_repeat_rows():
    df, contract = _fixture()
    res = DuplicatePctMetric().compute(df, contract, None)
    # a: 3 non-null, 3 distinct -> 0 dup rows -> 0%
    assert res.values["a"] == 0.0
    # b: 4 non-null, 2 distinct -> 2 dup rows out of 4 total -> 50%
    assert res.values["b"] == 50.0


def test_row_count_is_table_scope():
    df, contract = _fixture()
    res = RowCountMetric().compute(df, contract, None)
    assert res.scope == "table"
    assert res.values == {"__table__": 4}


def test_field_metric_handles_missing_column():
    df = pl.DataFrame({"a": [1, 2, 3]})
    contract = Contract.from_dict({
        "version": "1.0",
        "epic": "T",
        "table": "T",
        "spec": {"file_path": "s", "sheet_name": "T"},
        "fields": [
            {"name": "a", "type": "int32", "nullable": True},
            {"name": "missing", "type": "int32", "nullable": True},
        ],
    })
    res = NullCountMetric().compute(df, contract, None)
    # Missing column -> all "null" -> null_count == total rows
    assert res.values["a"] == 0
    assert res.values["missing"] == 3
