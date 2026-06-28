"""SQL predicate for allowed_values: rows whose value is non-null AND
not in the whitelist.

Mirrors dq_core.field_constraints.allowed_values.AllowedValuesConstraint.check_data.
Each whitelist entry is rendered as a single-quoted SQL string literal
with embedded single quotes doubled (standard SQL escape, accepted by
Trino).
"""

from __future__ import annotations


def predicate(field, check, dialect: str = "trino") -> str:
    """Return the SQL predicate that selects rows violating the whitelist."""
    quoted = ", ".join(_sql_string_literal(v) for v in (check.value or []))
    return (
        f'"{field.name}" IS NOT NULL '
        f'AND "{field.name}" NOT IN ({quoted})'
    )


def _sql_string_literal(value) -> str:
    """Render `value` as a single-quoted SQL string with embedded quotes
    doubled. Matches the standard SQL / Trino literal syntax."""
    return "'" + str(value).replace("'", "''") + "'"
