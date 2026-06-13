"""Table-check registry.

Mirror of `data_contract/field_constraints/` for whole-table checks. Adding
a new check:

1. Drop a new module here defining a subclass of `TableCheck`.
2. Register it in `_BUILTIN_MODULES` below (or call `register` manually).
3. Reference its `name` under `checks.table:` in `validation.yaml`.
"""

from __future__ import annotations

from importlib import import_module

from data_contract.errors import ConfigError
from data_contract.table_checks.base import TableCheck


REGISTRY: dict[str, type[TableCheck]] = {}


def register(cls: type[TableCheck]) -> type[TableCheck]:
    if not isinstance(cls.name, str) or not cls.name:
        raise ConfigError(f"TableCheck {cls.__qualname__} must declare a non-empty `name`")
    if not isinstance(cls.VIOLATION_KIND, str) or not cls.VIOLATION_KIND:
        raise ConfigError(
            f"TableCheck {cls.__qualname__} must declare a non-empty `VIOLATION_KIND`"
        )
    if not isinstance(cls.DIMENSION, str) or not cls.DIMENSION:
        raise ConfigError(
            f"TableCheck {cls.__qualname__} must declare a non-empty `DIMENSION` "
            f"(one of completeness/validity/uniqueness/consistency)"
        )
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ConfigError(
            f"TableCheck name {cls.name!r} already registered by "
            f"{REGISTRY[cls.name].__qualname__}"
        )
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[TableCheck]:
    if name not in REGISTRY:
        raise ConfigError(f"unknown table check {name!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[name]


_BUILTIN_MODULES = (
    "data_contract.table_checks.column_missing",
    "data_contract.table_checks.pk_uniqueness",
    "data_contract.table_checks.fk_existence",
)


def _load_builtins() -> None:
    for module_path in _BUILTIN_MODULES:
        mod = import_module(module_path)
        for cls in vars(mod).values():
            if (
                isinstance(cls, type)
                and issubclass(cls, TableCheck)
                and cls is not TableCheck
            ):
                if cls.name and cls.name not in REGISTRY:
                    register(cls)


_load_builtins()


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "TableCheck",
]
