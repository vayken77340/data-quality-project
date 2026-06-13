"""Null count per field."""

from __future__ import annotations

from data_contract.metrics.base import MetricResult, TableMetric


class NullCountMetric(TableMetric):
    name = "null_count"
    description = "Number of null values per field."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        values: dict[str, int] = {}
        for fc in contract.fields:
            if fc.name not in df.columns:
                values[fc.name] = df.height
                continue
            values[fc.name] = int(df[fc.name].is_null().sum())
        return MetricResult(name=self.name, scope=self.scope, values=values)
