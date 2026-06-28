"""Tests for the table_checks registry."""

from __future__ import annotations

import pytest

from dq_core.errors import ConfigError
from dq_core.table_checks import REGISTRY, get, register
from dq_core.table_checks.base import TableCheck


def test_load_builtins_populates_expected_names():
    expected = {"column_missing", "pk_uniqueness", "fk_existence"}
    assert expected <= set(REGISTRY)


def test_get_unknown_raises_config_error():
    with pytest.raises(ConfigError, match="unknown TableCheck"):
        get("not_a_check")


def test_register_requires_violation_kind():
    class NoKind(TableCheck):
        name = "no_kind_check"
        description = "x"
        DIMENSION = "validity"

        def check_data(self, frame, contract, **kw):
            return None

    try:
        with pytest.raises(ConfigError, match="`VIOLATION_KIND`"):
            register(NoKind)
    finally:
        REGISTRY.pop("no_kind_check", None)


def test_register_requires_dimension():
    class NoDim(TableCheck):
        name = "no_dim_check"
        description = "x"
        VIOLATION_KIND = "x_violation"

        def check_data(self, frame, contract, **kw):
            return None

    try:
        with pytest.raises(ConfigError, match="`DIMENSION`"):
            register(NoDim)
    finally:
        REGISTRY.pop("no_dim_check", None)


def test_register_rejects_double_registration_with_different_class():
    class A(TableCheck):
        name = "double_reg_tc"
        VIOLATION_KIND = "x"
        DIMENSION = "validity"

        def check_data(self, frame, contract, **kw):
            return None

    class B(TableCheck):
        name = "double_reg_tc"
        VIOLATION_KIND = "x"
        DIMENSION = "validity"

        def check_data(self, frame, contract, **kw):
            return None

    register(A)
    try:
        with pytest.raises(ConfigError, match="already registered"):
            register(B)
    finally:
        REGISTRY.pop("double_reg_tc", None)


def test_pk_uniqueness_has_correct_attributes():
    cls = REGISTRY["pk_uniqueness"]
    assert cls.VIOLATION_KIND == "pk_not_unique"
    assert cls.DIMENSION == "uniqueness"
    assert cls.scope == "row"
    assert cls.requires_cross_table is False


def test_fk_existence_marked_cross_table():
    cls = REGISTRY["fk_existence"]
    assert cls.requires_cross_table is True


def test_column_missing_is_table_scope():
    cls = REGISTRY["column_missing"]
    assert cls.scope == "table"
