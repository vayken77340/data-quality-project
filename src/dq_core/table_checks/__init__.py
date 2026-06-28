"""Table-check registry.

Adding a new table check:

1. Drop a new module here defining a subclass of `TableCheck`.
2. Register it in `_BUILTIN_MODULES` below (or call `register` manually).
3. Reference its `name` under `checks.table:` in `validation.yaml`.
"""

from __future__ import annotations

from dq_core.registry import BaseRegistry, RegistrySpec
from dq_core.table_checks.base import TableCheck


_BUILTIN_MODULES = (
    "dq_core.table_checks.column_missing",
    "dq_core.table_checks.pk_uniqueness",
    "dq_core.table_checks.fk_existence",
)


_R: BaseRegistry[TableCheck] = BaseRegistry(RegistrySpec(
    base_class=TableCheck,
    builtin_modules=_BUILTIN_MODULES,
    required_class_attrs=("name", "VIOLATION_KIND", "DIMENSION"),
))
_R.load_builtins()

REGISTRY = _R.REGISTRY
register = _R.register
get = _R.get


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "TableCheck",
]
