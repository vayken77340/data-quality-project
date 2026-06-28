"""SQL pushdown dispatch. Maps a FieldCheck's constraint name to the
predicate function that selects violating rows.

Returns None when no pushdown is registered for the check's constraint
class -- the runner skips those (a constraint with no SQL counterpart is
not a warehouse-side check).

Phase 2 registers: min_value, max_value, allowed_values.
"""

from __future__ import annotations

from typing import Callable

from warehouse_validation.sql_predicates import (
    allowed_values,
    max_value,
    min_value,
)


_DISPATCH: dict[str, Callable[..., str]] = {
    "min_value":      min_value.predicate,
    "max_value":      max_value.predicate,
    "allowed_values": allowed_values.predicate,
}


def to_sql_predicate(field, check, dialect: str = "trino") -> str | None:
    """Return the SQL predicate selecting rows that violate `check`, or
    None if the constraint has no SQL pushdown registered."""
    fn = _DISPATCH.get(check.constraint_cls.name)
    return fn(field, check, dialect) if fn else None


def supported_constraint_names() -> tuple[str, ...]:
    """Stable tuple of registered constraint names, for config / docs."""
    return tuple(sorted(_DISPATCH))


__all__ = ["to_sql_predicate", "supported_constraint_names"]
