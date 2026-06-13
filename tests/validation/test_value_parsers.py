from __future__ import annotations

from datetime import date, datetime

import pytest

from data_contract.type_mapping import Type
from data_contract.validation.checks.value_parsers import (
    get_parser,
    parse_boolean,
    parse_date,
    parse_double,
    parse_float,
    parse_integer,
    parse_timestamp,
    parse_varchar,
)


# --- varchar ----------------------------------------------------------------


def test_parse_varchar_identity():
    assert parse_varchar("hello") == ("hello", None)


def test_parse_varchar_preserves_leading_zeros():
    assert parse_varchar("00042") == ("00042", None)


def test_parse_varchar_none_passthrough():
    assert parse_varchar(None) == (None, None)


# --- integer ----------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("0", 0),
    ("42", 42),
    ("-7", -7),
    ("1234567890", 1234567890),
])
def test_parse_integer_happy(raw, expected):
    assert parse_integer(raw) == (expected, None)


@pytest.mark.parametrize("raw", [
    "42.0",          # whole-number float still rejected at INTEGER
    "12,34",         # French comma
    "1_000",         # Python underscore literal not accepted
    "1,000",         # US thousands separator
    " 42",           # leading whitespace
    "42 ",           # trailing whitespace
    "+42",           # explicit positive sign
    "0x2a",          # hex
    "",              # empty
    "abc",
])
def test_parse_integer_rejects(raw):
    value, err = parse_integer(raw)
    assert value is None
    assert err == raw


def test_parse_integer_none_passthrough():
    assert parse_integer(None) == (None, None)


# --- double / float ---------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("0", 0.0),
    ("42", 42.0),
    ("42.0", 42.0),
    ("3.14", 3.14),
    ("-2.5", -2.5),
    ("1e10", 1e10),
    ("1.5E-3", 1.5e-3),
    ("-0.0", -0.0),
])
def test_parse_double_happy(raw, expected):
    value, err = parse_double(raw)
    assert err is None
    assert value == expected


@pytest.mark.parametrize("raw", [
    "12,34",         # French decimal comma
    "1,000.5",       # US thousands separator
    ".5",            # leading-dot float not accepted
    "3.",            # trailing-dot float not accepted
    "inf",
    "NaN",
    "",
    "abc",
])
def test_parse_double_rejects(raw):
    value, err = parse_double(raw)
    assert value is None
    assert err == raw


def test_parse_float_aliased_to_float32():
    # parse_float is the back-compat alias for parse_float32; parse_double is
    # the back-compat alias for parse_float64. Both share the same regex but
    # different downstream Polars dtypes.
    from data_contract.validation.checks.value_parsers import (
        parse_float32, parse_float64,
    )
    assert parse_float is parse_float32
    assert parse_double is parse_float64


def test_parse_double_none_passthrough():
    assert parse_double(None) == (None, None)


# --- boolean ----------------------------------------------------------------


_BOOL_TOKENS = {
    "true": frozenset({"true", "vrai", "oui", "1", "y", "yes"}),
    "false": frozenset({"false", "faux", "non", "0", "n", "no"}),
}


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("  true  ", True),
    ("oui", True), ("OUI", True), ("yes", True), ("1", True), ("y", True),
    ("false", False), ("FALSE", False), ("non", False),
    ("no", False), ("0", False), ("n", False),
])
def test_parse_boolean_happy(raw, expected):
    assert parse_boolean(raw, tokens=_BOOL_TOKENS) == (expected, None)


@pytest.mark.parametrize("raw", ["maybe", "2", "", "vraii", "tru"])
def test_parse_boolean_rejects(raw):
    value, err = parse_boolean(raw, tokens=_BOOL_TOKENS)
    assert value is None
    assert err == raw


def test_parse_boolean_none_passthrough():
    assert parse_boolean(None, tokens=_BOOL_TOKENS) == (None, None)


def test_parse_boolean_empty_tokens_rejects_everything():
    value, err = parse_boolean("true", tokens={"true": frozenset(), "false": frozenset()})
    assert (value, err) == (None, "true")


# --- date -------------------------------------------------------------------


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")


def test_parse_date_first_format_matches():
    assert parse_date("2024-01-15", formats=_DATE_FORMATS) == (date(2024, 1, 15), None)


def test_parse_date_second_format_matches():
    assert parse_date("15/01/2024", formats=_DATE_FORMATS) == (date(2024, 1, 15), None)


def test_parse_date_all_formats_fail():
    value, err = parse_date("yesterday", formats=_DATE_FORMATS)
    assert value is None
    assert err == "yesterday"


def test_parse_date_invalid_calendar_value():
    # strptime catches this -- 2024-02-30 doesn't exist
    value, err = parse_date("2024-02-30", formats=_DATE_FORMATS)
    assert value is None
    assert err == "2024-02-30"


def test_parse_date_none_passthrough():
    assert parse_date(None, formats=_DATE_FORMATS) == (None, None)


def test_parse_date_empty_formats_rejects():
    value, err = parse_date("2024-01-15", formats=())
    assert (value, err) == (None, "2024-01-15")


# --- timestamp --------------------------------------------------------------


_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M")


def test_parse_timestamp_iso_t():
    assert parse_timestamp("2024-01-15T10:30:00", formats=_TS_FORMATS) == (
        datetime(2024, 1, 15, 10, 30, 0), None
    )


def test_parse_timestamp_iso_space():
    # This is what `str(datetime.datetime(...))` produces -- critical that the
    # default `parse_formats` list in configs/types.yaml includes this form.
    assert parse_timestamp("2024-01-15 10:30:00", formats=_TS_FORMATS) == (
        datetime(2024, 1, 15, 10, 30, 0), None
    )


def test_parse_timestamp_all_formats_fail():
    value, err = parse_timestamp("not a timestamp", formats=_TS_FORMATS)
    assert value is None
    assert err == "not a timestamp"


def test_parse_timestamp_none_passthrough():
    assert parse_timestamp(None, formats=_TS_FORMATS) == (None, None)


# --- get_parser dispatch ----------------------------------------------------


def test_get_parser_returns_callable_per_type():
    from data_contract.validation.checks.value_parsers import (
        parse_binary, parse_decimal, parse_float32, parse_float64,
        parse_int32, parse_int64, parse_string, parse_timestamp_tz,
    )
    assert get_parser(Type.STRING)       is parse_string
    assert get_parser(Type.INT32)        is parse_int32
    assert get_parser(Type.INT64)        is parse_int64
    assert get_parser(Type.FLOAT32)      is parse_float32
    assert get_parser(Type.FLOAT64)      is parse_float64
    assert get_parser(Type.DECIMAL)      is parse_decimal
    assert get_parser(Type.BOOLEAN)      is parse_boolean
    assert get_parser(Type.DATE)         is parse_date
    assert get_parser(Type.TIMESTAMP)    is parse_timestamp
    assert get_parser(Type.TIMESTAMP_TZ) is parse_timestamp_tz
    assert get_parser(Type.BINARY)       is parse_binary


def test_get_parser_unknown_returns_none():
    assert get_parser(Type.UNKNOWN) is None


# --- bounds (int32 / int64 / float32 / float64) -----------------------------


from data_contract.validation.checks.value_parsers import (
    parse_binary, parse_decimal, parse_float32, parse_float64,
    parse_int32, parse_int64, parse_string, parse_timestamp_tz,
)


def test_parse_int32_within_bounds():
    assert parse_int32("100", bounds=(-2**31, 2**31 - 1)) == (100, None)


def test_parse_int32_overflow_rejected():
    value, err = parse_int32("3000000000", bounds=(-2**31, 2**31 - 1))
    assert value is None
    assert err == "3000000000"


def test_parse_int32_underflow_rejected():
    value, err = parse_int32("-3000000000", bounds=(-2**31, 2**31 - 1))
    assert value is None
    assert err == "-3000000000"


def test_parse_int64_within_bounds():
    assert parse_int64("3000000000", bounds=(-2**63, 2**63 - 1)) == (3000000000, None)


def test_parse_int64_accepts_max_signed_64bit_with_bounds():
    """Bounds come from the target overlay; passing them explicitly accepts
    the 64-bit max value."""
    assert parse_int64("9223372036854775807", bounds=(-2**63, 2**63 - 1))[0] == 9223372036854775807


def test_parse_int_no_bounds_skips_range_check():
    """Without an active target -- which can't happen in normal validate-data
    flow since `target:` is required -- the parser only enforces the regex
    and skips the range check. Unit-test-only behaviour."""
    # 999999... matches the integer regex; with no bounds, it's accepted.
    value, err = parse_int64("999999999999999999999999")
    assert err is None
    assert value == 999999999999999999999999


def test_parse_int32_rejects_overflow_with_bounds():
    """The target-provided 32-bit bounds reject 3e9."""
    value, err = parse_int32("3000000000", bounds=(-2**31, 2**31 - 1))
    assert value is None
    assert err == "3000000000"


def test_parse_float32_bounds_violation():
    # Float32 max is ~3.4e38; pass a value just above.
    value, err = parse_float32("4e38", bounds=(-3.4e38, 3.4e38))
    assert value is None
    assert err == "4e38"


def test_parse_float64_bounds_pass():
    value, err = parse_float64("1.5e10", bounds=(-1e308, 1e308))
    assert err is None
    assert value == 1.5e10


# --- decimal ----------------------------------------------------------------


from decimal import Decimal


def test_parse_decimal_happy():
    value, err = parse_decimal("123.45", precision=5, scale=2)
    assert err is None
    assert value == Decimal("123.45")


def test_parse_decimal_integer_only():
    value, err = parse_decimal("42", precision=5, scale=2)
    assert err is None
    assert value == Decimal("42")


def test_parse_decimal_scale_overflow():
    # precision=5, scale=2 -> at most 2 fractional digits
    value, err = parse_decimal("1.234", precision=5, scale=2)
    assert value is None
    assert err == "1.234"


def test_parse_decimal_precision_overflow():
    # precision=5, scale=2 -> at most 3 integer digits
    value, err = parse_decimal("1234.5", precision=5, scale=2)
    assert value is None
    assert err == "1234.5"


def test_parse_decimal_zero_integer_part():
    # "0.50" has 0 effective integer digits; precision=2, scale=2 should accept.
    value, err = parse_decimal("0.50", precision=2, scale=2)
    assert err is None
    assert value == Decimal("0.50")


def test_parse_decimal_french_comma_rejected():
    value, err = parse_decimal("1,5", precision=5, scale=2)
    assert value is None
    assert err == "1,5"


def test_parse_decimal_without_precision_passes_through():
    # When precision/scale are None (no contract metadata), the regex still
    # gates correctness but no scale-overflow check runs.
    value, err = parse_decimal("1.234567890", precision=None, scale=None)
    assert err is None
    assert value == Decimal("1.234567890")


# --- timestamp_tz -----------------------------------------------------------


def test_parse_timestamp_tz_accepts_tz_aware():
    value, err = parse_timestamp_tz(
        "2024-01-15T10:30:00+0200", formats=("%Y-%m-%dT%H:%M:%S%z",),
    )
    assert err is None
    assert value.tzinfo is not None


def test_parse_timestamp_tz_rejects_naive():
    # Format without %z -> parses naive datetime -> rejected.
    value, err = parse_timestamp_tz(
        "2024-01-15T10:30:00", formats=("%Y-%m-%dT%H:%M:%S",),
    )
    assert value is None
    assert err == "2024-01-15T10:30:00"


def test_parse_timestamp_tz_all_formats_fail():
    value, err = parse_timestamp_tz(
        "garbage", formats=("%Y-%m-%dT%H:%M:%S%z",),
    )
    assert value is None
    assert err == "garbage"


# --- binary -----------------------------------------------------------------


def test_parse_binary_identity():
    assert parse_binary("anything") == ("anything", None)


def test_parse_binary_none_passthrough():
    assert parse_binary(None) == (None, None)


# --- string -----------------------------------------------------------------


def test_parse_string_ignores_kwargs():
    # max_length and length_unit are checked elsewhere (check_max_length).
    # parse_string is the identity parser.
    assert parse_string("00042", max_length=3, length_unit="bytes") == ("00042", None)
