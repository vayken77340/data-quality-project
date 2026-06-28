"""Trino connector. Lazy-imports the `trino` package so installations that
only run `validate-data` (file side) don't need to pull it in.

Config via env vars:
  TRINO_HOST     (required)
  TRINO_PORT     (default 443)
  TRINO_USER     (required)
  TRINO_CATALOG  (required)
  TRINO_SCHEMA   (optional)

Phase 2 opens one connection per CLI invocation and never closes it
explicitly -- the process exit reclaims the socket. A `close()` lifecycle
gets added in Phase 3+ when pooling actually matters.
"""

from __future__ import annotations

import os

from dq_core.errors import ConfigError
from warehouse_validation.connectors.base import Connector


class TrinoConnector(Connector):
    name = "trino"

    def __init__(self) -> None:
        try:
            import trino  # type: ignore[import-not-found]
        except ImportError as e:
            raise ConfigError(
                "trino connector requires the [validate-warehouse] extras. Install with:\n"
                "  pip install data-contract[validate-warehouse]\n"
                f"Details: {e}"
            ) from e

        host = _require_env("TRINO_HOST")
        user = _require_env("TRINO_USER")
        catalog = _require_env("TRINO_CATALOG")
        port = int(os.environ.get("TRINO_PORT", "443"))
        schema = os.environ.get("TRINO_SCHEMA")

        self._conn = trino.dbapi.connect(
            host=host, port=port, user=user, catalog=catalog, schema=schema,
            http_scheme="https",
        )

    def execute_scalar(self, sql: str) -> int | float | str | None:
        cur = self._conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None

    def execute_count(self, sql: str) -> int:
        return int(self.execute_scalar(sql) or 0)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"trino connector: required env var {name} is not set")
    return value
