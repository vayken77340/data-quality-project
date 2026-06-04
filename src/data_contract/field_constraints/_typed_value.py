from __future__ import annotations

from datetime import date, datetime
from typing import Any

from data_contract.type_mapping import Type


def parse_typed_value(raw: object, field_type: Type) -> tuple[Any | None, str | None]:
    """Coerce a raw cell value into the canonical Python type for `field_type`.

    Returns (value, None) on success or (None, message) on failure.
    Date/timestamp values are returned as canonical ISO-8601 strings rather than
    datetime/date objects so the contract YAML stays human-readable.
    String fields treat the raw as an integer character count.
    Bool and unknown types are not supported for min/max.
    """
    s = str(raw).strip()
    if field_type is Type.INTEGER:
        try:
            return int(s), None
        except ValueError:
            return None, f"value {raw!r} is not a valid integer"
    if field_type is Type.NUMBER:
        try:
            return float(s), None
        except ValueError:
            return None, f"value {raw!r} is not a valid number"
    if field_type is Type.STRING:
        try:
            n = int(s)
        except ValueError:
            return None, f"value {raw!r} is not a valid string length (expected an integer)"
        if n < 0:
            return None, f"string length bound {n} must be non-negative"
        return n, None
    if field_type is Type.DATE:
        try:
            return date.fromisoformat(s).isoformat(), None
        except ValueError:
            return None, f"value {raw!r} is not a valid ISO-8601 date"
    if field_type is Type.TIMESTAMP:
        try:
            return datetime.fromisoformat(s).isoformat(), None
        except ValueError:
            return None, f"value {raw!r} is not a valid ISO-8601 timestamp"
    return None, f"min/max not supported for field type {field_type.value!r}"
