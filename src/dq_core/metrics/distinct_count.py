"""Distinct non-null value count per field.

Polars `n_unique()` counts null as a distinct value; this metric subtracts
one when there are actual nulls so `distinct_count` means "distinct
non-null values" (the gold-standard data-profiling convention).
"""

from __future__ import annotations

from dq_core.metrics.base import MetricResult, TableMetric


class DistinctCountMetric(TableMetric):
    name = "distinct_count"
    description = "Number of distinct non-null values per field."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        values: dict[str, int] = {}
        for fc in contract.fields:
            if fc.silver_name not in df.columns:
                values[fc.silver_name] = 0
                continue
            col = df[fc.silver_name]
            null_count = int(col.is_null().sum())
            distinct = int(col.n_unique())
            if null_count > 0 and distinct > 0:
                distinct -= 1
            values[fc.silver_name] = distinct
        return MetricResult(name=self.name, scope=self.scope, values=values)
