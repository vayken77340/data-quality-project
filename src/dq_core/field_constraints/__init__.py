"""Field-constraint registry.

Adding a new constraint:

1. Drop a new module here defining a subclass of `FieldConstraint`.
2. Register it in `_BUILTIN_MODULES` below (or rely on `register` being called manually).
3. Reference its `name` in an epic's `defaults.yaml` under `column_mapping`.
"""

from __future__ import annotations

from dq_core.registry import BaseRegistry, IndexSpec, RegistrySpec
from dq_core.errors import ConfigError
from dq_core.field_constraints.base import (
    ConstraintContext,
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
    diff_numeric_bound,
    parse_bool,
    parse_column_ref,
)


_BUILTIN_MODULES = (
    "dq_core.field_constraints.unique",
    "dq_core.field_constraints.allowed_values",
    "dq_core.field_constraints.pattern",
    "dq_core.field_constraints.min_value",
    "dq_core.field_constraints.max_value",
    "dq_core.field_constraints.format",
    "dq_core.field_constraints.default_value",
)


def _validate_check_data_kind(cls: type[FieldConstraint]) -> None:
    """If a subclass overrides `check_data`, `VIOLATION_KIND` must be set.

    Constraints without a data-side check (e.g. default_value) may leave
    VIOLATION_KIND empty -- they're contract-only.
    """
    base_check = FieldConstraint.check_data
    own_check = cls.__dict__.get("check_data")
    if own_check is not None and own_check is not base_check:
        if not isinstance(cls.VIOLATION_KIND, str) or not cls.VIOLATION_KIND:
            raise ConfigError(
                f"FieldConstraint {cls.__qualname__} overrides `check_data` "
                f"but does not declare a non-empty `VIOLATION_KIND` class attribute"
            )


_R: BaseRegistry[FieldConstraint] = BaseRegistry(RegistrySpec(
    base_class=FieldConstraint,
    builtin_modules=_BUILTIN_MODULES,
    required_class_attrs=("name", "contract_key"),
    required_doc_headers=("Spec cell:", "Contract output:", "Drift:"),
    secondary_indexes=(
        IndexSpec(name="contract_key", attr="contract_key", unit="scalar"),
    ),
    extra_validator=_validate_check_data_kind,
))
_R.load_builtins()

REGISTRY = _R.REGISTRY
register = _R.register
get = _R.get


def constraint_for_contract_key(contract_key: str) -> type[FieldConstraint] | None:
    """Reverse-lookup a constraint class by its contract_key.

    The contract YAML stores constraints keyed by `contract_key` (e.g. `default`
    for the `default_value` constraint), but the registry indexes by `name`.
    This helper bridges the two.
    """
    return _R.get_by("contract_key", contract_key)


__all__ = [
    "REGISTRY",
    "register",
    "get",
    "constraint_for_contract_key",
    "FieldConstraint",
    "ConstraintContext",
    "DriftChange",
    "diff_added_or_removed",
    "diff_numeric_bound",
    "parse_bool",
    "parse_column_ref",
]
