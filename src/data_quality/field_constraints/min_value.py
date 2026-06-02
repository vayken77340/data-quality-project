from __future__ import annotations

from data_quality.field_constraints._typed_value import parse_typed_value
from data_quality.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_numeric_bound,
)


class MinValueConstraint(FieldConstraint):
    name = "min_value"
    contract_key = "min_value"

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        value, err_msg = parse_typed_value(raw_str, ctx.field_type)
        if err_msg is not None:
            return None, self._reject(
                "invalid_min_max", ctx, raw=raw_original,
                message=f"{self.name}: {err_msg}",
            )
        return value, None

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        # Raising a min bound is breaking; lowering it widens the accepted range.
        return diff_numeric_bound(
            field_name, old, new,
            name=cls.name,
            raise_severity="breaking",
            lower_severity="additive",
        )
