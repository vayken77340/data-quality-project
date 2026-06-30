"""Null count per field."""

from __future__ import annotations

from dq_core.metrics.base import MetricResult, TableMetric


class NullCountMetric(TableMetric):
    name = "null_count"
    description = "Number of null values per field."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        values: dict[str, int] = {}
        for fc in contract.fields:
            if fc.silver_name not in df.columns:
                values[fc.silver_name] = df.height
                continue
            values[fc.silver_name] = int(df[fc.silver_name].is_null().sum())
        return MetricResult(name=self.name, scope=self.scope, values=values)
