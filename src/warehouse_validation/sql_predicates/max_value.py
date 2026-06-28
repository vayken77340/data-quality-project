"""SQL predicate for max_value: rows violating the upper bound.

Mirrors dq_core.field_constraints.max_value.MaxValueConstraint.check_data:
non-null AND value above the threshold (strict: >=, non-strict: >).
"""

from __future__ import annotations


def predicate(field, check, dialect: str = "trino") -> str:
    """Return the SQL predicate that selects rows violating the max bound.

    Nulls are intentionally NOT flagged here -- nullability is a separate
    check (matches the Polars-side behaviour).
    """
    op = ">=" if check.params.get("strict") else ">"
    return f'"{field.name}" {op} {check.value}'
