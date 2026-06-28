"""Completeness percentage per field (100 - null%).

This is the dashboard-friendly inverse of `null_percentage` -- a higher
number means "more populated", matching the DAMA completeness dimension.
"""

from __future__ import annotations

from dq_core.metrics.base import MetricResult, TableMetric


class CompletenessMetric(TableMetric):
    name = "completeness"
    description = "Percentage of non-null values per field (0-100)."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        total = df.height
        values: dict[str, float] = {}
        for fc in contract.fields:
            if fc.name not in df.columns:
                values[fc.name] = 0.0
                continue
            null_count = int(df[fc.name].is_null().sum())
            values[fc.name] = round(((total - null_count) / total) * 100, 2) if total else 0.0
        return MetricResult(name=self.name, scope=self.scope, values=values)
