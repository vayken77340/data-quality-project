"""SQL predicate for allowed_values: rows whose value is non-null AND
not in the whitelist.

Mirrors dq_core.field_constraints.allowed_values.AllowedValuesConstraint.check_data.
Each whitelist entry is rendered as a single-quoted SQL string literal
with embedded single quotes doubled (standard SQL escape, accepted by
Trino and Oracle).
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL, sql_string_literal


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return the WHERE fragment that selects rows violating the whitelist.

    `table_name` is unused; kept for dispatch signature uniformity
    with `unique`.
    """
    quoted = ", ".join(sql_string_literal(v) for v in (check.value or []))
    return PushdownSQL(
        kind="where",
        sql=(
            f'"{field.silver_name}" IS NOT NULL '
            f'AND "{field.silver_name}" NOT IN ({quoted})'
        ),
    )
