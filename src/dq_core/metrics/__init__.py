"""Metrics registry.

Adding a new metric:

1. Drop a new module here defining a subclass of `TableMetric`.
2. Register it in `_BUILTIN_MODULES` below (or call `register` manually).
3. Reference its `name` under `metrics.field:` or `metrics.table:`
   (matching the metric's `scope`) in `validation.yaml`.
"""

from __future__ import annotations

from dq_core.registry import BaseRegistry, RegistrySpec
from dq_core.errors import ConfigError
from dq_core.metrics.base import MetricResult, TableMetric


_BUILTIN_MODULES = (
    "dq_core.metrics.null_count",
    "dq_core.metrics.null_percentage",
    "dq_core.metrics.distinct_count",
    "dq_core.metrics.completeness",
    "dq_core.metrics.duplicate_pct",
    "dq_core.metrics.row_count",
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
