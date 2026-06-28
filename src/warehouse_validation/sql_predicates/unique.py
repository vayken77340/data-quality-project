"""SQL pushdown for `unique`: count rows whose value collides with
another row's value.

Mirrors dq_core.field_constraints.unique.UniqueConstraint.check_data,
which joins the frame back to its own duplicate keys -- meaning every
ROW in a collision group is flagged (a value appearing N times
contributes N violations, not 1).

This shape doesn't fit the WHERE-clause mould the other predicates
use, so it returns `kind="count_query"` and the runner executes the
SQL verbatim. The predicate needs `table_name` to interpolate the
subquery target.

Portable: GROUP BY + HAVING + IN-subquery are standard SQL accepted by
Trino, Oracle, and DuckDB.
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return a full COUNT(*) query for rows whose value participates
    in a duplicate-value collision."""
    col = field.name
    return PushdownSQL(
        kind="count_query",
        sql=(
            f'SELECT COUNT(*) FROM "{table_name}" '
            f'WHERE "{col}" IS NOT NULL '
            f'AND "{col}" IN ('
            f'SELECT "{col}" FROM "{table_name}" '
            f'WHERE "{col}" IS NOT NULL '
            f'GROUP BY "{col}" '
            f'HAVING COUNT(*) > 1'
            f')'
        ),
    )
