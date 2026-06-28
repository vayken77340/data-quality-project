"""Shared utility helpers for SQL predicate compilers.

  * `PushdownSQL`     -- the structured return type from every predicate
                          function. Lives here (not in __init__.py) so
                          predicate modules can import it without a
                          circular import.
  * `sql_string_literal` -- single-quoted SQL string with embedded
                          quotes doubled. Used by allowed_values,
                          format, pattern to render values safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PushdownSQL:
    """One scored SQL pushdown for a constraint.

    kind="where"        -- runner wraps `sql` in
                            `SELECT COUNT(*) FROM "<table>" WHERE <sql>`.
    kind="count_query"  -- runner executes `sql` verbatim; it MUST be
                            a complete `SELECT COUNT(*) FROM ...`.
    """
    kind: Literal["where", "count_query"]
    sql: str


def sql_string_literal(value) -> str:
    """Render `value` as a single-quoted SQL string with embedded quotes
    doubled. Matches the standard SQL / Trino / Oracle literal syntax."""
    return "'" + str(value).replace("'", "''") + "'"
