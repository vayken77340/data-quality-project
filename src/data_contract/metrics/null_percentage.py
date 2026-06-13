"""Null percentage per field (0.0 - 100.0)."""

from __future__ import annotations

from data_contract.metrics.base import MetricResult, TableMetric


class NullPercentageMetric(TableMetric):
    name = "null_percentage"
    description = "Percentage of null values per field (0-100)."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        total = df.height
        values: dict[str, float] = {}
        for fc in contract.fields:
            if fc.name not in df.columns:
                values[fc.name] = 100.0 if total else 0.0
                continue
            null_count = int(df[fc.name].is_null().sum())
            values[fc.name] = round((null_count / total) * 100, 2) if total else 0.0
        return MetricResult(name=self.name, scope=self.scope, values=values)
