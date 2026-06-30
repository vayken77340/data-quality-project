"""SQL predicate for `format`: rows whose value doesn't match the
named format's regex.

Mirrors dq_core.field_constraints.format.FormatConstraint.check_data.
`check.value` is a token name (e.g. "email") looked up in
FORMAT_REGISTRY; tokens without a `pattern` (informational-only) have
no SQL counterpart -- returns None to match the Polars-side behaviour.
Unknown token names also return None.

`regexp_like` is portable across Trino and Oracle. DuckDB exposes a
differently named function (`regexp_matches`), so format / pattern
predicates are excluded from the DuckDB-substrate parity test.
"""

from __future__ import annotations

from dq_core.field_constraints.format import FORMAT_REGISTRY

from warehouse_validation.sql_predicates._util import PushdownSQL, sql_string_literal


def predicate(field, check, table_name, dialect: str = "trino") -> PushdownSQL | None:
    """Return the WHERE fragment for the named format's regex, or
    None when the token is unknown / informational-only."""
    token_info = FORMAT_REGISTRY.get(str(check.value))
    if token_info is None or token_info.pattern is None:
        return None
    quoted = sql_string_literal(token_info.pattern)
    return PushdownSQL(
        kind="where",
        sql=(
            f'"{field.silver_name}" IS NOT NULL '
            f'AND NOT regexp_like("{field.silver_name}", {quoted})'
        ),
    )
