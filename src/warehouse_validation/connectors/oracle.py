"""Oracle connector. Lazy-imports `oracledb` so installations that only
run validate-data (or trino-only validate-warehouse) don't pull the
client in.

Config via env vars:
  ORACLE_USER       (required)
  ORACLE_PASSWORD   (required)
  ORACLE_DSN        (required)  e.g. `host:port/service_name`
  ORACLE_SCHEMA     (optional)  default schema for bare-name lookup;
                                 defaults to the connected user

Oracle has no catalog layer; the bare-name fallback resolves against
the schema env (or the connecting user when ORACLE_SCHEMA is unset).
Identifiers go to ALL_TAB_COLUMNS upper-cased because Oracle stores
unquoted identifiers in upper case.

The Trino connector's no-explicit-close lifecycle is mirrored here:
process exit reclaims the connection. Add a close() lifecycle if a
later phase pools connections across runs.
"""

from __future__ import annotations

import os

from dq_core.errors import ConfigError
from warehouse_validation.connectors.base import Connector


class OracleConnector(Connector):
    name = "oracle"
    dialect = "oracle"

    def __init__(self) -> None:
        try:
            import oracledb  # type: ignore[import-not-found]
        except ImportError as e:
            raise ConfigError(
                "oracle connector requires the [validate-warehouse-oracle] extras. "
                "Install with:\n"
                "  pip install data-contract[validate-warehouse-oracle]\n"
                f"Details: {e}"
            ) from e

        user = _require_env("ORACLE_USER")
        password = _require_env("ORACLE_PASSWORD")
        dsn = _require_env("ORACLE_DSN")
        # Default schema = connected user; ORACLE_SCHEMA overrides.
        self.schema: str = os.environ.get("ORACLE_SCHEMA") or user

        self._conn = oracledb.connect(user=user, password=password, dsn=dsn)

    def execute_scalar(self, sql: str) -> int | float | str | None:
        cur = self._conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None

    def execute_count(self, sql: str) -> int:
        return int(self.execute_scalar(sql) or 0)

    def execute_columns(self, fq_table: str) -> set[str]:
        sch, tbl = self._split_fq(fq_table)
        # ALL_TAB_COLUMNS lists every column the connected user can see.
        # Oracle stores unquoted identifiers in upper case; case-fold inputs
        # to match.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT column_name FROM all_tab_columns "
            "WHERE owner = :owner AND table_name = :tname",
            owner=sch.upper(), tname=tbl.upper(),
        )
        return {row[0] for row in cur}

    def _split_fq(self, fq_table: str) -> tuple[str, str]:
        parts = fq_table.split(".")
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            return self.schema, parts[0]
        raise ConfigError(
            f"oracle connector: table name {fq_table!r} must be either "
            f"bare `<table>` or `<schema>.<table>` (Oracle has no catalog layer)"
        )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"oracle connector: required env var {name} is not set")
    return value
