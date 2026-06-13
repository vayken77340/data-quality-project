"""Metrics registry.

Mirrors `data_contract/field_constraints/` and `data_contract/table_checks/`.
Adding a new metric:

1. Drop a new module here defining a subclass of `TableMetric`.
2. Register it in `_BUILTIN_MODULES` below (or call `register` manually).
3. Reference its `name` under `metrics.field:` or `metrics.table:` (matching
   the metric's `scope`) in `validation.yaml`.
"""

from __future__ import annotations

from importlib import import_module

from data_contract.errors import ConfigError
from data_contract.metrics.base import MetricResult, TableMetric


REGISTRY: dict[str, type[TableMetric]] = {}


def register(cls: type[TableMetric]) -> type[TableMetric]:
    if not isinstance(cls.name, str) or not cls.name:
        raise ConfigError(f"TableMetric {cls.__qualname__} must declare a non-empty `name`")
    if cls.scope not in {"field", "table"}:
        raise ConfigError(
            f"TableMetric {cls.__qualname__} must declare `scope` as 'field' or 'table'; "
            f"got {cls.scope!r}"
        )
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ConfigError(
            f"TableMetric name {cls.name!r} already registered by "
            f"{REGISTRY[cls.name].__qualname__}"
        )
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[TableMetric]:
    if name not in REGISTRY:
        raise ConfigError(f"unknown metric {name!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[name]


_BUILTIN_MODULES = (
    "data_contract.metrics.null_count",
    "data_contract.metrics.null_percentage",
    "data_contract.metrics.distinct_count",
    "data_contract.metrics.completeness",
    "data_contract.metrics.duplicate_pct",
    "data_contract.metrics.row_count",
)


def _load_builtins() -> None:
    for module_path in _BUILTIN_MODULES:
        mod = import_module(module_path)
        for cls in vars(mod).values():
            if (
                isinstance(cls, type)
                and issubclass(cls, TableMetric)
                and cls is not TableMetric
            ):
                if cls.name and cls.name not in REGISTRY:
                    register(cls)


_load_builtins()


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "TableMetric",
    "MetricResult",
]
