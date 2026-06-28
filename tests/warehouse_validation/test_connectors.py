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
    def execute(self, sql):
        self._last_sql = sql

    def fetchone(self):
        return (42,)


class _FakeTrinoConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def cursor(self):
        return _FakeCursor()


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
        get_connector("oracle")
    assert "oracle" in str(exc.value)
    assert "trino" in str(exc.value)


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
