"""Shared helpers for violation aggregation.

Reused by every report writer that needs:
  * a top-N breakdown of which offending values drove a violation kind
    (HTML, JSON, Markdown) -- `aggregate_top_values`.
  * a per-severity violation count -- `count_severity`.
  * a per-dimension filtered score -- `filtered_dimension_score`,
    used by XLSX's Summary scorecard for completeness/uniqueness/etc.

One implementation per primitive = identical numbers across formats.
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


def count_severity(violations: Iterable[Violation]) -> dict[str, int]:
    """Bucket counts for the three canonical severities.

    Output is always `{"error": N, "warning": N, "info": N}`; unknown
    severities are silently dropped.
    """
    out = {"error": 0, "warning": 0, "info": 0}
    for v in violations:
        if v.severity in out:
            out[v.severity] += 1
    return out


def filtered_dimension_score(
    violations: Iterable[Violation], *, kinds: frozenset[str], total_rows: int,
) -> float:
    """Per-dimension pass rate considering only error-severity violations in `kinds`.

    Row-based formula matching the overall score: distinct
    (source_file, source_row) tuples with at least one matching error mark
    the row dirty. Empty tables score 0.0 (no rows to be clean).
    """
    if total_rows <= 0:
        return 0.0
    dirty: set[tuple] = set()
    for v in violations:
        if v.severity != "error" or v.kind not in kinds:
            continue
        if v.source_file is not None and v.source_row is not None:
            dirty.add((v.source_file, v.source_row))
    clean = max(total_rows - len(dirty), 0)
    return round(clean / total_rows * 100, 2)


def _stringify(value: Any) -> str:
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return repr(value)
