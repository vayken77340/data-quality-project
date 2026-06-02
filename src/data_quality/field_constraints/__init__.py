"""Field-constraint registry.

Adding a new constraint:
1. Drop a new module here defining a subclass of `FieldConstraint`.
2. Register it in `_BUILTIN_MODULES` below (or rely on `register` being called manually).
3. Reference its `name` in an epic's `defaults.yaml` under `column_mapping`.
"""

from __future__ import annotations

from importlib import import_module

from data_quality.errors import ConfigError
from data_quality.field_constraints.base import (
    ConstraintColumnRef,
    ConstraintContext,
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
    diff_numeric_bound,
    parse_bool,
    parse_column_ref,
)


REGISTRY: dict[str, type[FieldConstraint]] = {}


def register(cls: type[FieldConstraint]) -> type[FieldConstraint]:
    if not isinstance(cls.name, str) or not cls.name:
        raise ConfigError(f"FieldConstraint {cls.__qualname__} must declare a non-empty `name`")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ConfigError(
            f"FieldConstraint name {cls.name!r} already registered by {REGISTRY[cls.name].__qualname__}"
        )
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[FieldConstraint]:
    if name not in REGISTRY:
        raise ConfigError(f"unknown field constraint {name!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[name]


_BUILTIN_MODULES = (
    "data_quality.field_constraints.unique",
    "data_quality.field_constraints.allowed_values",
    "data_quality.field_constraints.pattern",
    "data_quality.field_constraints.min_value",
    "data_quality.field_constraints.max_value",
)


def _load_builtins() -> None:
    for module_path in _BUILTIN_MODULES:
        mod = import_module(module_path)
        for cls in vars(mod).values():
            if isinstance(cls, type) and issubclass(cls, FieldConstraint) and cls is not FieldConstraint:
                if cls.name and cls.name not in REGISTRY:
                    register(cls)


_load_builtins()


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "FieldConstraint",
    "ConstraintColumnRef",
    "ConstraintContext",
    "DriftChange",
    "diff_added_or_removed",
    "diff_numeric_bound",
    "parse_bool",
    "parse_column_ref",
]
