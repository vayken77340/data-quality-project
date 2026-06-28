"""Connector registry + Trino factory tests. No live Trino: monkeypatches
the dbapi `connect()` call so the import-and-construct path is exercised
without a network call.
"""

from __future__ import annotations

import sys
import types

import pytest

from dq_core.errors import ConfigError
from warehouse_validation.connectors import (
    CONNECTOR_REGISTRY,
    get_connector,
)
from warehouse_validation.connectors.base import Connector
from warehouse_validation.connectors.trino import TrinoConnector


class _FakeCursor:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = list(rows) if rows is not None else [(42,)]

    def execute(self, sql):
        self._last_sql = sql

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeTrinoConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        # Rows the next cursor will return. Tests poke this between calls.
        self.next_rows: list[tuple] | None = None
        self.last_sql: str | None = None

    def cursor(self):
        cur = _FakeCursor(self.next_rows)
        # Record the SQL after execute() runs, via the cursor's _last_sql.
        original_execute = cur.execute

        def _spy_execute(sql):
            self.last_sql = sql
            original_execute(sql)

        cur.execute = _spy_execute  # type: ignore[method-assign]
        return cur


@pytest.fixture
def _stub_trino_package(monkeypatch):
    """Install a fake `trino` module before TrinoConnector imports it."""
    fake_trino = types.ModuleType("trino")
    fake_dbapi = types.ModuleType("trino.dbapi")
    fake_dbapi.connect = lambda **kwargs: _FakeTrinoConnection(**kwargs)
    fake_trino.dbapi = fake_dbapi
    monkeypatch.setitem(sys.modules, "trino", fake_trino)
    monkeypatch.setitem(sys.modules, "trino.dbapi", fake_dbapi)
    return fake_dbapi


@pytest.fixture
def _trino_env(monkeypatch):
    monkeypatch.setenv("TRINO_HOST", "warehouse.example.com")
    monkeypatch.setenv("TRINO_USER", "dq_runner")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.delenv("TRINO_PORT", raising=False)
    monkeypatch.delenv("TRINO_SCHEMA", raising=False)


def test_registry_has_trino():
    assert CONNECTOR_REGISTRY["trino"] is TrinoConnector


def test_get_connector_trino_returns_instance(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert isinstance(conn, Connector)
    assert isinstance(conn, TrinoConnector)


def test_get_connector_unknown_name_raises_config_error():
    with pytest.raises(ConfigError) as exc:
        get_connector("snowflake")
    msg = str(exc.value)
    assert "snowflake" in msg
    # Error lists both supported connectors so the operator can pick one.
    assert "trino" in msg
    assert "oracle" in msg


def test_trino_execute_count_unwraps_scalar(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert conn.execute_count("SELECT COUNT(*) FROM t") == 42


def test_trino_execute_scalar_returns_first_cell(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert conn.execute_scalar("SELECT 42") == 42


def test_trino_missing_required_env_raises(_stub_trino_package, monkeypatch):
    monkeypatch.delenv("TRINO_HOST", raising=False)
    monkeypatch.setenv("TRINO_USER", "u")
    monkeypatch.setenv("TRINO_CATALOG", "c")
    with pytest.raises(ConfigError) as exc:
        TrinoConnector()
    assert "TRINO_HOST" in str(exc.value)


def test_trino_missing_package_raises_helpful_config_error(monkeypatch, _trino_env):
    # Force ImportError by stubbing `trino` to a sentinel that raises on attribute access.
    monkeypatch.setitem(sys.modules, "trino", None)
    with pytest.raises(ConfigError) as exc:
        TrinoConnector()
    assert "validate-warehouse" in str(exc.value)


# -- execute_columns ----------------------------------------------------------


def test_connector_subclass_missing_execute_columns_fails_instantiation():
    class Half(Connector):
        def execute_scalar(self, sql):  # noqa: ARG002
            return None

        def execute_count(self, sql):  # noqa: ARG002
            return 0
        # no execute_columns -- ABC should refuse to instantiate this.

    with pytest.raises(TypeError) as exc:
        Half()  # type: ignore[abstract]
    assert "execute_columns" in str(exc.value)


def test_trino_exposes_catalog_and_schema(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    assert conn.catalog == "iceberg"
    assert conn.schema is None


def test_trino_execute_columns_fq_name_parses_three_parts(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    # Wire fake cursor to return two column rows.
    conn._conn.next_rows = [("col_a",), ("col_b",)]  # type: ignore[attr-defined]
    cols = conn.execute_columns("warehouse.gold.projects")
    assert cols == {"col_a", "col_b"}
    sent = conn._conn.last_sql  # type: ignore[attr-defined]
    assert "warehouse.information_schema.columns" in sent
    assert "table_schema = 'gold'" in sent
    assert "table_name = 'projects'" in sent


def test_trino_execute_columns_bare_name_uses_default_catalog_schema(
    _stub_trino_package, monkeypatch,
):
    monkeypatch.setenv("TRINO_HOST", "warehouse.example.com")
    monkeypatch.setenv("TRINO_USER", "dq_runner")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.setenv("TRINO_SCHEMA", "silver")
    monkeypatch.delenv("TRINO_PORT", raising=False)
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    conn._conn.next_rows = [("id",)]  # type: ignore[attr-defined]
    cols = conn.execute_columns("projects_bronze")
    assert cols == {"id"}
    sent = conn._conn.last_sql  # type: ignore[attr-defined]
    assert "iceberg.information_schema.columns" in sent
    assert "table_schema = 'silver'" in sent
    assert "table_name = 'projects_bronze'" in sent


def test_trino_execute_columns_bare_name_without_schema_raises(
    _stub_trino_package, _trino_env,
):
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    # _trino_env deletes TRINO_SCHEMA, so the bare-name path can't resolve.
    with pytest.raises(ConfigError) as exc:
        conn.execute_columns("bare_name")
    assert "TRINO_SCHEMA" in str(exc.value)


def test_trino_execute_columns_two_part_name_raises(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    with pytest.raises(ConfigError) as exc:
        conn.execute_columns("sch.tbl")
    assert "bare" in str(exc.value) or "fully-qualified" in str(exc.value)


def test_trino_execute_columns_escapes_single_quote(_stub_trino_package, _trino_env):
    conn = get_connector("trino")
    assert isinstance(conn, TrinoConnector)
    conn._conn.next_rows = []  # type: ignore[attr-defined]
    conn.execute_columns("cat.sch.o'mara")
    sent = conn._conn.last_sql  # type: ignore[attr-defined]
    assert "'o''mara'" in sent


# -- FakeConnector ------------------------------------------------------------


def test_fake_connector_execute_columns_returns_configured_set(fake_connector_factory):
    fc = fake_connector_factory(
        columns_by_table={"cat.sch.projects": {"pk", "amount"}},
    )
    assert fc.execute_columns("cat.sch.projects") == {"pk", "amount"}
    assert fc.column_lookups == ["cat.sch.projects"]


def test_fake_connector_execute_columns_unknown_table_returns_empty_set(
    fake_connector_factory,
):
    fc = fake_connector_factory()
    assert fc.execute_columns("missing") == set()
    assert fc.column_lookups == ["missing"]


def test_fake_connector_dialect_defaults_to_trino(fake_connector_factory):
    fc = fake_connector_factory()
    assert fc.dialect == "trino"


def test_fake_connector_dialect_override_to_oracle(fake_connector_factory):
    fc = fake_connector_factory(dialect="oracle")
    assert fc.dialect == "oracle"


# -- Oracle connector ---------------------------------------------------------


from warehouse_validation.connectors.oracle import OracleConnector


class _FakeOracleCursor:
    """Minimal cursor that records the SQL + bind kwargs from execute()."""

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = list(rows) if rows is not None else [(7,)]
        self.last_sql: str | None = None
        self.last_kwargs: dict | None = None

    def execute(self, sql, **kwargs):
        self.last_sql = sql
        self.last_kwargs = kwargs

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeOracleConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.next_rows: list[tuple] | None = None
        self._last_cursor: _FakeOracleCursor | None = None

    def cursor(self):
        cur = _FakeOracleCursor(self.next_rows)
        self._last_cursor = cur
        return cur


@pytest.fixture
def _stub_oracledb_package(monkeypatch):
    fake_oracledb = types.ModuleType("oracledb")
    fake_oracledb.connect = lambda **kwargs: _FakeOracleConnection(**kwargs)
    monkeypatch.setitem(sys.modules, "oracledb", fake_oracledb)
    return fake_oracledb


@pytest.fixture
def _oracle_env(monkeypatch):
    monkeypatch.setenv("ORACLE_USER", "dq_app")
    monkeypatch.setenv("ORACLE_PASSWORD", "secret")
    monkeypatch.setenv("ORACLE_DSN", "warehouse.example.com:1521/prod")
    monkeypatch.delenv("ORACLE_SCHEMA", raising=False)


def test_registry_has_oracle():
    assert CONNECTOR_REGISTRY["oracle"] is OracleConnector


def test_get_connector_oracle_returns_instance(_stub_oracledb_package, _oracle_env):
    conn = get_connector("oracle")
    assert isinstance(conn, Connector)
    assert isinstance(conn, OracleConnector)
    assert conn.dialect == "oracle"


def test_oracle_default_schema_is_user(_stub_oracledb_package, _oracle_env):
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    assert conn.schema == "dq_app"


def test_oracle_schema_env_overrides_user(_stub_oracledb_package, monkeypatch):
    monkeypatch.setenv("ORACLE_USER", "dq_app")
    monkeypatch.setenv("ORACLE_PASSWORD", "secret")
    monkeypatch.setenv("ORACLE_DSN", "warehouse:1521/prod")
    monkeypatch.setenv("ORACLE_SCHEMA", "REPORTING")
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    assert conn.schema == "REPORTING"


def test_oracle_missing_required_env_raises(_stub_oracledb_package, monkeypatch):
    monkeypatch.delenv("ORACLE_USER", raising=False)
    monkeypatch.setenv("ORACLE_PASSWORD", "secret")
    monkeypatch.setenv("ORACLE_DSN", "warehouse:1521/prod")
    with pytest.raises(ConfigError) as exc:
        OracleConnector()
    assert "ORACLE_USER" in str(exc.value)


def test_oracle_missing_package_raises_helpful_config_error(monkeypatch, _oracle_env):
    monkeypatch.setitem(sys.modules, "oracledb", None)
    with pytest.raises(ConfigError) as exc:
        OracleConnector()
    msg = str(exc.value)
    assert "validate-warehouse-oracle" in msg


def test_oracle_execute_columns_two_part_name(_stub_oracledb_package, _oracle_env):
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    conn._conn.next_rows = [("PROJ_ID",), ("AMOUNT",)]  # type: ignore[attr-defined]
    cols = conn.execute_columns("reporting.projects")
    assert cols == {"PROJ_ID", "AMOUNT"}
    cur = conn._conn._last_cursor  # type: ignore[attr-defined]
    assert cur is not None
    assert "all_tab_columns" in cur.last_sql
    # Bind params are upper-cased to match Oracle's stored identifier case.
    assert cur.last_kwargs == {"owner": "REPORTING", "tname": "PROJECTS"}


def test_oracle_execute_columns_bare_name_uses_default_schema(
    _stub_oracledb_package, _oracle_env,
):
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    conn._conn.next_rows = [("PK",)]  # type: ignore[attr-defined]
    conn.execute_columns("orders")
    cur = conn._conn._last_cursor  # type: ignore[attr-defined]
    assert cur.last_kwargs == {"owner": "DQ_APP", "tname": "ORDERS"}


def test_oracle_execute_columns_three_part_name_raises(_stub_oracledb_package, _oracle_env):
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    with pytest.raises(ConfigError) as exc:
        conn.execute_columns("cat.sch.tbl")
    msg = str(exc.value)
    assert "no catalog layer" in msg


def test_oracle_execute_count_unwraps_scalar(_stub_oracledb_package, _oracle_env):
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)
    conn._conn.next_rows = [(99,)]  # type: ignore[attr-defined]
    assert conn.execute_count("SELECT COUNT(*) FROM projects") == 99
