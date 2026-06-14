"""Tests for the metrics registry."""

from __future__ import annotations

import pytest

from data_contract.errors import ConfigError
from data_contract.metrics import REGISTRY, get, register
from data_contract.metrics.base import TableMetric


def test_load_builtins_populates_expected_names():
    """Every built-in metric registered itself at module import time."""
    expected = {
        "null_count", "null_percentage", "distinct_count",
        "completeness", "duplicate_pct", "row_count",
    }
    assert expected <= set(REGISTRY)


def test_get_unknown_raises_config_error():
    with pytest.raises(ConfigError, match="unknown TableMetric"):
        get("not_a_metric")


def test_register_rejects_unknown_scope():
    class Bad(TableMetric):
        name = "bad_scope_metric"
        scope = "not-a-scope"

        def compute(self, df, contract, type_registry):
            raise NotImplementedError

    try:
        with pytest.raises(ConfigError, match="must declare `scope`"):
            register(Bad)
    finally:
        REGISTRY.pop("bad_scope_metric", None)


def test_register_rejects_empty_name():
    class Anon(TableMetric):
        scope = "field"

        def compute(self, df, contract, type_registry):
            raise NotImplementedError

    with pytest.raises(ConfigError, match="non-empty `name`"):
        register(Anon)


def test_register_rejects_double_registration_with_different_class():
    """Two classes claiming the same name raises -- the registry is the
    single source of truth for name -> class."""
    class A(TableMetric):
        name = "double_reg_test"
        scope = "field"

        def compute(self, df, contract, type_registry):
            raise NotImplementedError

    class B(TableMetric):
        name = "double_reg_test"
        scope = "field"

        def compute(self, df, contract, type_registry):
            raise NotImplementedError

    register(A)
    try:
        with pytest.raises(ConfigError, match="already registered"):
            register(B)
    finally:
        REGISTRY.pop("double_reg_test", None)
