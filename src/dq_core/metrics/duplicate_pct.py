"""Duplicate percentage per field.

Definition: among non-null values, the share that appear more than once
in the column. Computed as `(non_null - distinct_non_null) / total * 100`,
which is the share of rows whose value is NOT the first occurrence of a
distinct value. Useful as a generic "how unique is this column" gauge.
"""

from __future__ import annotations

from dq_core.metrics.base import MetricResult, TableMetric


class DuplicatePctMetric(TableMetric):
    name = "duplicate_pct"
    description = "Percentage of rows whose value duplicates an earlier row's value."
    scope = "field"

    def compute(self, df, contract, type_registry) -> MetricResult:
        total = df.height
        values: dict[str, float] = {}
        for fc in contract.fields:
            if fc.name not in df.columns or total == 0:
                values[fc.name] = 0.0
                continue
            col = df[fc.name]
            null_count = int(col.is_null().sum())
            distinct = int(col.n_unique())
            if null_count > 0 and distinct > 0:
                distinct -= 1
            non_null = total - null_count
            dup_rows = max(non_null - distinct, 0)
            values[fc.name] = round((dup_rows / total) * 100, 2)
        return MetricResult(name=self.name, scope=self.scope, values=values)
