"""SQL pushdown dispatch. Maps a FieldCheck's constraint name to the
SQL fragment (or full count query) that scores the violating rows.

Returns None when no pushdown is registered for the check's constraint
class -- the runner skips those (a constraint with no SQL counterpart
is not a warehouse-side check).

Two pushdown shapes (defined on `PushdownSQL` in `_util.py`):

  * `kind="where"`        -- runner wraps in
                              `SELECT COUNT(*) FROM "<table>" WHERE <sql>`.
                              Most constraints.
  * `kind="count_query"`  -- runner executes verbatim. Used by `unique`
                              (subquery counts distinct duplicate values).
"""

from __future__ import annotations

from typing import Callable

from warehouse_validation.sql_predicates._util import PushdownSQL
from warehouse_validation.sql_predicates import (
    allowed_values,
    format as _format,
    max_value,
    min_value,
    pattern as _pattern,
)


_DISPATCH: dict[str, Callable[..., "PushdownSQL | None"]] = {
    "min_value":      min_value.predicate,
    "max_value":      max_value.predicate,
    "allowed_values": allowed_values.predicate,
    "format":         _format.predicate,
    "pattern":        _pattern.predicate,
}


def to_sql_pushdown(
    field, check, table_name: str, dialect: str = "trino",
) -> PushdownSQL | None:
    """Return the SQL pushdown for `check`, or None when no pushdown
    is registered for its constraint class.

    `table_name` is needed by predicates whose SQL embeds the table
    (e.g. `unique`'s subquery). Predicates that only need a WHERE
    fragment ignore it.
    """
    fn = _DISPATCH.get(check.constraint_cls.name)
    return fn(field, check, table_name, dialect) if fn else None


def supported_constraint_names() -> tuple[str, ...]:
    """Stable tuple of registered constraint names, for config / docs."""
    return tuple(sorted(_DISPATCH))


__all__ = ["PushdownSQL", "to_sql_pushdown", "supported_constraint_names"]
