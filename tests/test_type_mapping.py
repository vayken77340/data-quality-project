from pathlib import Path

import pytest

from data_quality.type_mapping import Type, load_type_registry, parse_type, unknown_parsed_type


@pytest.fixture(scope="module")
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_double_to_number(registry):
    parsed, err = parse_type("Double", registry, sheet_row=2)
    assert err is None
    assert parsed.type is Type.NUMBER
    assert parsed.max_length is None


def test_boolean_french(registry):
    parsed, err = parse_type("Booléen", registry, sheet_row=3)
    assert err is None
    assert parsed.type is Type.BOOLEAN


def test_timestamp_french(registry):
    parsed, err = parse_type("Date horodatée", registry, sheet_row=4)
    assert err is None
    assert parsed.type is Type.TIMESTAMP


def test_varchar_with_length(registry):
    parsed, err = parse_type("VARCHAR(384)", registry, sheet_row=5)
    assert err is None
    assert parsed.type is Type.STRING
    assert parsed.max_length == 384


def test_varchar_with_whitespace(registry):
    parsed, err = parse_type("VARCHAR (384)", registry, sheet_row=6)
    assert err is None
    assert parsed.type is Type.STRING
    assert parsed.max_length == 384


def test_decimal_precision_scale(registry):
    parsed, err = parse_type("decimal(10,2)", registry, sheet_row=7)
    assert err is None
    assert parsed.type is Type.NUMBER
    assert parsed.precision == 10
    assert parsed.scale == 2


def test_bigint(registry):
    parsed, err = parse_type("BIGINT", registry, sheet_row=8)
    assert err is None
    assert parsed.type is Type.INTEGER


def test_text(registry):
    parsed, err = parse_type("text", registry, sheet_row=9)
    assert err is None
    assert parsed.type is Type.STRING


def test_unknown_type_returns_rejection(registry):
    parsed, err = parse_type("Quaternion(8)", registry, sheet_row=10)
    assert parsed is None
    assert err is not None
    assert err.kind == "unknown_type"
    assert err.sheet_row == 10


def test_unknown_parsed_type_helper():
    parsed = unknown_parsed_type()
    assert parsed.type is Type.UNKNOWN
