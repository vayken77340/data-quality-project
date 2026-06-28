"""SQL predicate for min_value: rows violating the lower bound.

Mirrors dq_core.field_constraints.min_value.MinValueConstraint.check_data:
non-null AND value below the threshold (strict: <=, non-strict: <).
"""

from __future__ import annotations


def predicate(field, check, dialect: str = "trino") -> str:
    """Return the SQL predicate that selects rows violating the min bound.

    Nulls are intentionally NOT flagged here -- nullability is a separate
    check (matches the Polars-side behaviour).
    """
    op = "<=" if check.params.get("strict") else "<"
    return f'"{field.name}" {op} {check.value}'
