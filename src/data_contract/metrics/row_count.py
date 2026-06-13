"""Total row count -- table-scope."""

from __future__ import annotations

from data_contract.metrics.base import TABLE_SCOPE_KEY, MetricResult, TableMetric


class RowCountMetric(TableMetric):
    name = "row_count"
    description = "Total number of rows in the table."
    scope = "table"

    def compute(self, df, contract, type_registry) -> MetricResult:
        return MetricResult(
            name=self.name,
            scope=self.scope,
            values={TABLE_SCOPE_KEY: df.height},
        )
