"""Tests for the canonical Type -> Trino CAST string mapping."""

from __future__ import annotations

import pytest

from dq_core.errors import ConfigError
from dq_core.type_mapping import Type
from warehouse_validation.type_coercion import _TRINO_CAST, trino_cast_type


@pytest.mark.parametrize("t,expected", [
    (Type.INT64,     "BIGINT"),
    (Type.FLOAT64,   "DOUBLE"),
    (Type.BOOLEAN,   "BOOLEAN"),
    (Type.STRING,    "VARCHAR"),
    (Type.DATE,      "DATE"),
    (Type.TIMESTAMP, "TIMESTAMP"),
])
def test_supported_types_map_to_expected_cast(t, expected):
    assert trino_cast_type(t) == expected


def test_every_supported_type_round_trips_through_table():
    for t, cast in _TRINO_CAST.items():
        assert trino_cast_type(t) == cast


@pytest.mark.parametrize("t", [Type.UNKNOWN, Type.BINARY, Type.DECIMAL])
def test_unmapped_type_raises_config_error(t):
    with pytest.raises(ConfigError) as exc:
        trino_cast_type(t)
    msg = str(exc.value)
    assert "no Trino CAST type" in msg
    # error message lists supported types so the operator can see what's available
    for supported in (k.value for k in _TRINO_CAST):
        assert supported in msg
