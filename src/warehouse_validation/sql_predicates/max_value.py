"""SQL predicate for max_value: rows violating the upper bound.

Mirrors dq_core.field_constraints.max_value.MaxValueConstraint.check_data:
non-null AND value above the threshold (strict: >=, non-strict: >).
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return the WHERE fragment that selects rows violating the max bound.

    Nulls are intentionally NOT flagged here -- nullability is a
    separate check (matches the Polars-side behaviour). `table_name`
    is unused; kept for dispatch signature uniformity with `unique`.
    """
    op = ">=" if check.params.get("strict") else ">"
    return PushdownSQL(kind="where", sql=f'"{field.name}" {op} {check.value}')
