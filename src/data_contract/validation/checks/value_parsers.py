"""Per-canonical-type string parsers used by `check_type_coercion` and
`normalize_typed_column`.

Each parser takes a raw string (or None) plus type-specific kwargs, and
returns `(parsed_value, error_token)`:

  - `raw is None` -> `(None, None)`. Null is handled by `check_nullable`,
    not a coercion failure.
  - Successful parse -> `(value, None)`.
  - Failure -> `(None, raw)`. The offending text is returned verbatim so
    violations can quote it.

The pure-Python design means these are unit-testable without Polars and
without any file-format dependency. They run identically on strings sourced
from CSV (`pl.scan_csv(infer_schema=False)`) or from Excel (calamine cells
stringified via `str()`).

`bounds`, `precision`/`scale`, and `length_unit` kwargs reflect target-overlay
choices made by `TypeRegistry.with_target(...)`. When no target is configured,
callers pass `bounds=None` and the bound-check is skipped.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from data_contract.type_mapping import Type


ParseResult = tuple[Any | None, str | None]


# Strict ASCII numeric regexes. No thousands separators, no European commas.
# These are the contract's authority on what counts as an integer / float.
_INTEGER_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


# ---------------------------------------------------------------------------
# String
# ---------------------------------------------------------------------------


def parse_string(
    raw: str | None,
    *,
    max_length: int | None = None,
    length_unit: str = "characters",
) -> ParseResult:
    """Identity. Length is enforced by `check_max_length`; both kwargs are
    accepted here so the dispatch in `core_fields._parser_kwargs` is uniform.
    """
    if raw is None:
        return None, None
    return raw, None


# ---------------------------------------------------------------------------
# Integers (int32 / int64)
# ---------------------------------------------------------------------------

# Bounds come solely from the target overlay. There is no "intrinsic" range
# baked into the parser: the contract's `int32` is a universal logical type
# and only acquires a concrete range once a target maps it to a physical type
# (Postgres INTEGER -> 32-bit; Oracle NUMBER(10) -> 32-bit; etc.). Every
# target YAML MUST declare bounds for every numeric canonical it overrides.


def _within(value, bounds: tuple[Any, Any] | None) -> bool:
    if bounds is None:
        # No bounds means "no target rule for this type." The contract layer
        # is expected to require a target, so this path only fires in unit
        # tests that explicitly skip bounds.
        return True
    lo, hi = bounds
    return lo <= value <= hi


def parse_int32(raw: str | None, *, bounds: tuple[Any, Any] | None = None) -> ParseResult:
    if raw is None:
        return None, None
    if not _INTEGER_RE.match(raw):
        return None, raw
    value = int(raw)
    if not _within(value, bounds):
        return None, raw
    return value, None


def parse_int64(raw: str | None, *, bounds: tuple[Any, Any] | None = None) -> ParseResult:
    if raw is None:
        return None, None
    if not _INTEGER_RE.match(raw):
        return None, raw
    value = int(raw)
    if not _within(value, bounds):
        return None, raw
    return value, None


# ---------------------------------------------------------------------------
# Floats (float32 / float64)
# ---------------------------------------------------------------------------


def parse_float32(raw: str | None, *, bounds: tuple[Any, Any] | None = None) -> ParseResult:
    if raw is None:
        return None, None
    if not _FLOAT_RE.match(raw):
        return None, raw
    value = float(raw)
    if not _within(value, bounds):
        return None, raw
    return value, None


def parse_float64(raw: str | None, *, bounds: tuple[Any, Any] | None = None) -> ParseResult:
    if raw is None:
        return None, None
    if not _FLOAT_RE.match(raw):
        return None, raw
    value = float(raw)
    if not _within(value, bounds):
        return None, raw
    return value, None


# ---------------------------------------------------------------------------
# Decimal
# ---------------------------------------------------------------------------


# Decimal regex: strict ASCII, optional sign, optional fractional part. Same
# spirit as the float regex but without scientific notation -- decimals are
# explicit, not e-notated.
_DECIMAL_RE = re.compile(r"^-?(\d+)(\.(\d+))?$")


def parse_decimal(
    raw: str | None,
    *,
    precision: int | None,
    scale: int | None,
    bounds: tuple[Any, Any] | None = None,
) -> ParseResult:
    """Parse a string to a `decimal.Decimal`.

    Rejects when:
      - the regex doesn't match
      - integer digits exceed `(precision - scale)` (e.g. DECIMAL(5,2) rejects "1234.5")
      - fractional digits exceed `scale` (DECIMAL(5,2) rejects "1.234")
      - the value is outside the optional `bounds`
    """
    if raw is None:
        return None, None
    m = _DECIMAL_RE.match(raw)
    if not m:
        return None, raw
    int_digits = m.group(1)
    frac_digits = m.group(3) or ""
    # Allow precision/scale to be None when no contract metadata is available
    # (e.g. unit-testing the parser in isolation).
    if scale is not None and len(frac_digits) > scale:
        return None, raw
    if precision is not None and scale is not None:
        max_int_digits = precision - scale
        # Strip a leading zero on a pure-integer (e.g. "0.5" should not count as 1 int digit).
        normalized_int = int_digits.lstrip("0") or "0"
        if normalized_int == "0":
            int_count = 0
        else:
            int_count = len(normalized_int)
        if int_count > max_int_digits:
            return None, raw
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, raw
    if bounds is not None:
        lo, hi = bounds
        if not (Decimal(str(lo)) <= value <= Decimal(str(hi))):
            return None, raw
    return value, None


# ---------------------------------------------------------------------------
# Boolean (unchanged)
# ---------------------------------------------------------------------------


def parse_boolean(raw: str | None, *, tokens: Mapping[str, frozenset[str]]) -> ParseResult:
    if raw is None:
        return None, None
    norm = raw.strip().lower()
    if norm in tokens.get("true", frozenset()):
        return True, None
    if norm in tokens.get("false", frozenset()):
        return False, None
    return None, raw


# ---------------------------------------------------------------------------
# Date / Timestamp / Timestamp_tz
# ---------------------------------------------------------------------------


def parse_date(raw: str | None, *, formats: Sequence[str]) -> ParseResult:
    if raw is None:
        return None, None
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date(), None
        except ValueError:
            continue
    return None, raw


def parse_timestamp(raw: str | None, *, formats: Sequence[str]) -> ParseResult:
    if raw is None:
        return None, None
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt), None
        except ValueError:
            continue
    return None, raw


def parse_timestamp_tz(raw: str | None, *, formats: Sequence[str]) -> ParseResult:
    """Like parse_timestamp but the parsed datetime MUST carry a timezone.

    A format that doesn't include `%z` will succeed at strptime but produce a
    naive datetime; that's flagged as a violation.
    """
    if raw is None:
        return None, None
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return parsed, None
    return None, raw


# ---------------------------------------------------------------------------
# Binary
# ---------------------------------------------------------------------------


def parse_binary(raw: str | None) -> ParseResult:
    """Identity. v1 doesn't enforce hex/base64 encoding -- contract authors
    declaring binary today are signalling 'arbitrary opaque bytes'.
    """
    if raw is None:
        return None, None
    return raw, None


_PARSER_BY_TYPE: dict[Type, Callable[..., ParseResult]] = {
    Type.STRING:       parse_string,
    Type.TEXT:         parse_string,    # unbounded variable-length: same parse path
    Type.INT32:        parse_int32,
    Type.INT64:        parse_int64,
    Type.FLOAT32:      parse_float32,
    Type.FLOAT64:      parse_float64,
    Type.DECIMAL:      parse_decimal,
    Type.BOOLEAN:      parse_boolean,
    Type.DATE:         parse_date,
    Type.TIMESTAMP:    parse_timestamp,
    Type.TIMESTAMP_TZ: parse_timestamp_tz,
    Type.BINARY:       parse_binary,
}


def get_parser(t: Type) -> Callable[..., ParseResult] | None:
    """Return the parser callable for a canonical type, or None for UNKNOWN.

    Callers must supply the right kwargs per type:
      - STRING:            optional `max_length`, `length_unit`
      - INT32 / INT64:     optional `bounds`
      - FLOAT32 / FLOAT64: optional `bounds`
      - DECIMAL:           `precision`, `scale`, optional `bounds`
      - BOOLEAN:           `tokens=registry.data_values_for(Type.BOOLEAN)`
      - DATE / TIMESTAMP / TIMESTAMP_TZ: `formats=registry.parse_formats_for(field.type)`
      - BINARY:            no kwargs
    """
    return _PARSER_BY_TYPE.get(t)
