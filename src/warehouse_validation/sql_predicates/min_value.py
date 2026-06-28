"""SQL predicate for min_value: rows violating the lower bound.

Mirrors dq_core.field_constraints.min_value.MinValueConstraint.check_data:
non-null AND value below the threshold (strict: <=, non-strict: <).
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return the WHERE fragment that selects rows violating the min bound.

    Nulls are intentionally NOT flagged here -- nullability is a
    separate check (matches the Polars-side behaviour). `table_name`
    is unused; kept for dispatch signature uniformity with `unique`.
    """
    op = "<=" if check.params.get("strict") else "<"
    return PushdownSQL(kind="where", sql=f'"{field.name}" {op} {check.value}')
