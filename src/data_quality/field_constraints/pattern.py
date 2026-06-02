from __future__ import annotations

import re

from data_quality.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class PatternConstraint(FieldConstraint):
    name = "pattern"
    contract_key = "pattern"

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
