"""Shared fixtures for parser tests.

Parsers no longer carry per-format defaults on the class -- defaults live
in `configs/parsers.yaml`. These helpers reproduce that loading in tests
so each `CsvParser(...)` / `ExcelParser(...)` instantiation gets the same
baseline a production runner would supply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_contract.data_parsers import load_parser_yaml_overrides


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PARSERS_YAML = _REPO_ROOT / "configs" / "parsers.yaml"


@pytest.fixture(scope="session")
def parser_yaml_defaults() -> dict[str, dict]:
    """The per-format defaults block from `configs/parsers.yaml`."""
    return load_parser_yaml_overrides(_PARSERS_YAML)


@pytest.fixture(scope="session")
def csv_params(parser_yaml_defaults):
    """Factory: returns CSV defaults merged with caller-supplied overrides.

    Usage:
        parser = CsvParser(csv_params())                 # YAML defaults only
        parser = CsvParser(csv_params(delimiter=";"))    # YAML + override
    """
    base = parser_yaml_defaults.get("csv", {})
    def factory(**overrides):
        return {**base, **overrides}
    return factory


@pytest.fixture(scope="session")
def excel_params(parser_yaml_defaults):
    """Factory: returns Excel defaults merged with caller-supplied overrides."""
    base = parser_yaml_defaults.get("excel", {})
    def factory(**overrides):
        return {**base, **overrides}
    return factory


@pytest.fixture(scope="session")
def json_params(parser_yaml_defaults):
    """Factory: returns JSON defaults merged with caller-supplied overrides."""
    base = parser_yaml_defaults.get("json", {})
    def factory(**overrides):
        return {**base, **overrides}
    return factory
