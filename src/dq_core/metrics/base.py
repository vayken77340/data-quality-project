"""Base classes for the pluggable metrics registry.

A `TableMetric` computes a single descriptive statistic over a table
frame. Two scopes:

* `"field"` -- emits a value per contract field. `compute` returns
               `dict[field_name, value]`.
* `"table"` -- emits a single value for the whole table. `compute`
               returns `{"__table__": value}`.

Metrics are reported alongside violations: violations answer "what is
broken?", metrics answer "what does the data look like?".

Adding a new metric:
1. Drop a new module under `dq_core/metrics/` defining a subclass
   of `TableMetric` with `name`, `scope`, `description`.
2. Register it in `_BUILTIN_MODULES` in `__init__.py` (or call
   `register(MyMetric)` from another package).
3. Add an on/off entry under `metrics.field:` or `metrics.table:` in
   `validation.yaml` (matching the metric's `scope`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar


# Sentinel key used for table-scope metric values.
TABLE_SCOPE_KEY = "__table__"


@dataclass(frozen=True)
class MetricResult:
    """One metric's computed values for one table.

    For field-scope metrics, `values` is `{field_name: value}`. For
    table-scope metrics, it's `{"__table__": value}`.
    """
    name: str
    scope: str
    values: dict[str, Any]


class TableMetric(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    scope: ClassVar[str] = "field"   # "field" or "table"

    @abstractmethod
    def compute(self, df, contract, type_registry) -> MetricResult:
        """Compute the metric over `df` (eager Polars DataFrame).

        Implementations MUST lazy-import polars inside the method body so
        the base `data_contract` package stays Polars-free at import time.
        """
