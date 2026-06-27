"""Shared fixtures for parser tests.

Tests are HERMETIC: defaults declared here do not come from production
`configs/parsers.yaml`. A test that wants to exercise a specific delimiter,
encoding, or null-token set passes it as an explicit override to the factory.

The production YAML is exercised only by `tests/data_parsers/test_yaml_overrides.py`,
which uses its own tmp_path YAML rather than reading the repo's file.

Baseline values mirror what every test fixture in this directory writes
(comma-separated CSVs, UTF-8, ", " null tokens). Changing them is a test-only
decision and should not affect production code.
"""

from __future__ import annotations

import pytest


_CSV_DEFAULTS = {
    "encoding":              "utf-8",
    "delimiter":             ",",
    "header_row":            1,
    "null_tokens":           ["", "NULL"],
    "quote_char":            '"',
    "field_matching_policy": "positional",
}

_EXCEL_DEFAULTS = {
    "header_row":            1,
    "null_tokens":           ["", "NULL"],
    "field_matching_policy": "positional",
}

_JSON_DEFAULTS = {
    "encoding":              "utf-8",
    "header_path":           "data[0].report_header",
    "rows_path":             "data[0].report_row",
    "name_key":              "name",
    "null_tokens":           ["", "null", "NULL"],
    "field_matching_policy": "exact",
}


@pytest.fixture(scope="session")
def csv_params():
    """Factory: returns CSV defaults merged with caller-supplied overrides.

    Usage:
        parser = CsvParser(csv_params())                 # hermetic baseline
        parser = CsvParser(csv_params(delimiter=";"))    # override one knob
    """
    def factory(**overrides):
        return {**_CSV_DEFAULTS, **overrides}
    return factory


@pytest.fixture(scope="session")
def excel_params():
    """Factory: returns Excel defaults merged with caller-supplied overrides."""
    def factory(**overrides):
        return {**_EXCEL_DEFAULTS, **overrides}
    return factory


@pytest.fixture(scope="session")
def json_params():
    """Factory: returns JSON defaults merged with caller-supplied overrides."""
    def factory(**overrides):
        return {**_JSON_DEFAULTS, **overrides}
    return factory
