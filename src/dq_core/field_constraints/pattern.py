from __future__ import annotations

import re

from dq_core.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class PatternConstraint(FieldConstraint):
    """A raw regex that values for a field must match.

    Spec cell:    a regex string; validated by `re.compile` at parse time.
    Contract output: flat string `pattern: <regex>`.
    Drift:        added/changed = breaking; removed = additive.
    """

    name = "pattern"
    contract_key = "pattern"

    CONTRACT_VALUE_SCHEMA = {"type": "string", "minLength": 1}

    VIOLATION_KIND = "pattern_violation"

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        try:
            re.compile(raw_str)
        except re.error as e:
            return None, self._reject(
                "invalid_pattern",
                ctx,
                raw=raw_original,
                message=f"pattern {raw_str!r} does not compile: {e}",
            )
        return raw_str, None

    @classmethod
    def check_data(cls, frame, field, check):
        """Flag rows whose value is non-null AND does not match the regex."""
        import polars as pl

        regex = str(check.value)
        col = pl.col(field.silver_name).cast(pl.String, strict=False)
        return frame.filter(col.is_not_null() & ~col.str.contains(regex))

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        if old == new:
            return None
        edge = diff_added_or_removed(
            field_name, old, new,
            added_kind="pattern_added", added_severity="breaking",
            removed_kind="pattern_removed", removed_severity="additive",
        )
        if edge is not None:
            return edge
        return DriftChange(
            kind="pattern_changed",
            severity="breaking",
            field=field_name,
            detail={"from": old, "to": new},
        )
