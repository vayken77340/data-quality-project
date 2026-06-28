"""Tests for the canonical Type -> per-dialect CAST string mapping."""

from __future__ import annotations

import pytest

from dq_core.errors import ConfigError
from dq_core.type_mapping import Type
from warehouse_validation.type_coercion import (
    _CAST_BY_DIALECT,
    _ORACLE_CAST,
    _TRINO_CAST,
    cast_type,
)


# -- trino dialect ------------------------------------------------------------


@pytest.mark.parametrize("t,expected", [
    (Type.INT64,     "BIGINT"),
    (Type.FLOAT64,   "DOUBLE"),
    (Type.BOOLEAN,   "BOOLEAN"),
    (Type.STRING,    "VARCHAR"),
    (Type.DATE,      "DATE"),
    (Type.TIMESTAMP, "TIMESTAMP"),
])
def test_trino_supported_types(t, expected):
    assert cast_type(t, "trino") == expected


# -- oracle dialect -----------------------------------------------------------


@pytest.mark.parametrize("t,expected", [
    (Type.INT64,     "NUMBER(38)"),
    (Type.FLOAT64,   "BINARY_DOUBLE"),
    (Type.BOOLEAN,   "NUMBER(1)"),
    (Type.STRING,    "VARCHAR2(4000)"),
    (Type.DATE,      "DATE"),
    (Type.TIMESTAMP, "TIMESTAMP(6)"),
])
def test_oracle_supported_types(t, expected):
    assert cast_type(t, "oracle") == expected


# -- round-trips through the tables ------------------------------------------


def test_trino_table_round_trips():
    for t, c in _TRINO_CAST.items():
        assert cast_type(t, "trino") == c


def test_oracle_table_round_trips():
    for t, c in _ORACLE_CAST.items():
        assert cast_type(t, "oracle") == c


# -- error paths -------------------------------------------------------------


@pytest.mark.parametrize("t", [Type.UNKNOWN, Type.BINARY, Type.DECIMAL])
def test_unmapped_trino_type_raises_config_error(t):
    with pytest.raises(ConfigError) as exc:
        cast_type(t, "trino")
    msg = str(exc.value)
    assert "no trino CAST type" in msg
    for supported in (k.value for k in _TRINO_CAST):
        assert supported in msg


@pytest.mark.parametrize("t", [Type.UNKNOWN, Type.BINARY, Type.DECIMAL])
def test_unmapped_oracle_type_raises_config_error(t):
    with pytest.raises(ConfigError) as exc:
        cast_type(t, "oracle")
    msg = str(exc.value)
    assert "no oracle CAST type" in msg


def test_unknown_dialect_raises_config_error():
    with pytest.raises(ConfigError) as exc:
        cast_type(Type.INT64, "snowflake")
    msg = str(exc.value)
    assert "snowflake" in msg
    assert "trino" in msg
    assert "oracle" in msg


def test_cast_by_dialect_registry_lists_both():
    assert set(_CAST_BY_DIALECT) == {"trino", "oracle"}
