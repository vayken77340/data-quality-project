"""Field-constraint registry.

Adding a new constraint:
1. Drop a new module here defining a subclass of `FieldConstraint`.
2. Register it in `_BUILTIN_MODULES` below (or rely on `register` being called manually).
3. Reference its `name` in an epic's `defaults.yaml` under `column_mapping`.
"""

from __future__ import annotations

from importlib import import_module

from data_contract.errors import ConfigError
from data_contract.field_constraints.base import (
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
_CONTRACT_KEY_INDEX: dict[str, type[FieldConstraint]] = {}


def constraint_for_contract_key(contract_key: str) -> type[FieldConstraint] | None:
    """Reverse-lookup a constraint class by its contract_key.

    The contract YAML stores constraints keyed by `contract_key` (e.g. `default`
    for the `default_value` constraint), but the registry indexes by `name`.
    This helper bridges the two without re-scanning REGISTRY on every call.

    Defensively rechecks the primary REGISTRY so tests that pop entries out by
    name don't leave stale reverse-index hits.
    """
    cls = _CONTRACT_KEY_INDEX.get(contract_key)
    if cls is None:
        return None
    if REGISTRY.get(cls.name) is not cls:
        _CONTRACT_KEY_INDEX.pop(contract_key, None)
        return None
    return cls


_REQUIRED_DOCSTRING_HEADERS = ("Spec cell:", "Contract output:", "Drift:")


def register(cls: type[FieldConstraint]) -> type[FieldConstraint]:
    if not isinstance(cls.name, str) or not cls.name:
        raise ConfigError(f"FieldConstraint {cls.__qualname__} must declare a non-empty `name`")
    if not isinstance(cls.contract_key, str) or not cls.contract_key:
        raise ConfigError(f"FieldConstraint {cls.__qualname__} must declare a non-empty `contract_key`")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ConfigError(
            f"FieldConstraint name {cls.name!r} already registered by {REGISTRY[cls.name].__qualname__}"
        )
    for existing_name, existing_cls in REGISTRY.items():
        if existing_cls is cls:
            continue
        if existing_cls.contract_key == cls.contract_key:
            raise ConfigError(
                f"FieldConstraint {cls.__qualname__} contract_key {cls.contract_key!r} "
                f"is already used by {existing_cls.__qualname__} (name={existing_name!r}); "
                f"two constraints sharing a contract_key would corrupt the drift index"
            )
    doc = (cls.__doc__ or "").strip()
    if not doc:
        raise ConfigError(
            f"FieldConstraint {cls.__qualname__} must have a non-empty docstring; "
            f"document its spec cell shape, contract output shape, and drift severity policy"
        )
    missing_headers = [h for h in _REQUIRED_DOCSTRING_HEADERS if h not in doc]
    if missing_headers:
        raise ConfigError(
            f"FieldConstraint {cls.__qualname__} docstring must include the headers "
            f"{list(_REQUIRED_DOCSTRING_HEADERS)}; missing: {missing_headers}"
        )
    REGISTRY[cls.name] = cls
    _CONTRACT_KEY_INDEX[cls.contract_key] = cls
    return cls


def get(name: str) -> type[FieldConstraint]:
    if name not in REGISTRY:
        raise ConfigError(f"unknown field constraint {name!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[name]


_BUILTIN_MODULES = (
    "data_contract.field_constraints.unique",
    "data_contract.field_constraints.allowed_values",
    "data_contract.field_constraints.pattern",
    "data_contract.field_constraints.min_value",
    "data_contract.field_constraints.max_value",
    "data_contract.field_constraints.format",
    "data_contract.field_constraints.default_value",
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
    "constraint_for_contract_key",
    "FieldConstraint",
    "ConstraintColumnRef",
    "ConstraintContext",
    "DriftChange",
    "diff_added_or_removed",
    "diff_numeric_bound",
    "parse_bool",
    "parse_column_ref",
]
