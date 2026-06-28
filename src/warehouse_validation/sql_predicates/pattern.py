"""SQL predicate for `pattern`: rows whose value doesn't match the
contract's raw regex.

Mirrors dq_core.field_constraints.pattern.PatternConstraint.check_data,
which uses Polars' `str.contains(regex)` against the field column.
`check.value` is the raw regex string; it gets inlined as a SQL string
literal with embedded single quotes doubled.

regexp_like is portable Trino <-> Oracle. DuckDB uses regexp_matches;
this predicate is intentionally outside Stream C's parity matrix.
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL, sql_string_literal


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return the WHERE fragment that selects rows whose value is
    non-null AND fails the regex."""
    quoted = sql_string_literal(check.value)
    return PushdownSQL(
        kind="where",
        sql=(
            f'"{field.name}" IS NOT NULL '
            f'AND NOT regexp_like("{field.name}", {quoted})'
        ),
    )
