"""Metrics registry.

Adding a new metric:

1. Drop a new module here defining a subclass of `TableMetric`.
2. Register it in `_BUILTIN_MODULES` below (or call `register` manually).
3. Reference its `name` under `metrics.field:` or `metrics.table:`
   (matching the metric's `scope`) in `validation.yaml`.
"""

from __future__ import annotations

from data_contract.core.registry import BaseRegistry, RegistrySpec
from data_contract.errors import ConfigError
from data_contract.metrics.base import MetricResult, TableMetric


_BUILTIN_MODULES = (
    "data_contract.metrics.null_count",
    "data_contract.metrics.null_percentage",
    "data_contract.metrics.distinct_count",
    "data_contract.metrics.completeness",
    "data_contract.metrics.duplicate_pct",
    "data_contract.metrics.row_count",
)


def _validate_scope(cls: type[TableMetric]) -> None:
    if cls.scope not in {"field", "table"}:
        raise ConfigError(
            f"TableMetric {cls.__qualname__} must declare `scope` as "
            f"'field' or 'table'; got {cls.scope!r}"
        )


_R: BaseRegistry[TableMetric] = BaseRegistry(RegistrySpec(
    base_class=TableMetric,
    builtin_modules=_BUILTIN_MODULES,
    required_class_attrs=("name",),
    extra_validator=_validate_scope,
))
_R.load_builtins()

REGISTRY = _R.REGISTRY
register = _R.register
get = _R.get


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "TableMetric",
    "MetricResult",
]
