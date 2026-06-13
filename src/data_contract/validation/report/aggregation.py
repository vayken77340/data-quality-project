"""Shared helpers for value-level violation aggregation.

Reused by every report writer that needs a top-N breakdown of which
offending values drove a violation kind (HTML, JSON, Markdown). One
implementation = identical numbers across formats.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, NamedTuple

from data_contract.violations import Violation


_NULL_SENTINEL = "(null)"


class TopValues(NamedTuple):
    top: list[tuple[str, int]]   # [(stringified_value, count), ...] sorted desc
    remaining_rows: int          # rows whose value did NOT make the top list
    remaining_distinct: int      # distinct values not in the top list


def aggregate_top_values(violations: Iterable[Violation], *, n: int = 10) -> TopValues:
    counter: Counter[str] = Counter()
    for v in violations:
        counter[_stringify(v.offending_value)] += 1
    most_common = counter.most_common(n)
    top_distinct = {value for value, _ in most_common}
    remaining_rows = sum(c for v, c in counter.items() if v not in top_distinct)
    remaining_distinct = sum(1 for v in counter if v not in top_distinct)
    return TopValues(
        top=[(value, count) for value, count in most_common],
        remaining_rows=remaining_rows,
        remaining_distinct=remaining_distinct,
    )


def _stringify(value: Any) -> str:
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return repr(value)
