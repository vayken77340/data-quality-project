from __future__ import annotations

from data_quality.field_constraints._typed_value import parse_typed_value
from data_quality.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_numeric_bound,
)
from data_quality.type_mapping import Type


class MaxValueConstraint(FieldConstraint):
    name = "max_value"
    contract_key = "max_value"

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        value, err_msg = parse_typed_value(raw_str, ctx.field_type)
        if err_msg is not None:
            return None, self._reject(
                "invalid_min_max", ctx, raw=raw_original,
                message=f"{self.name}: {err_msg}",
            )
        # For string fields, max_value is a length cap; reject if it exceeds
        # the type-derived max_length, which would be a contradiction.
        if (
            ctx.field_type is Type.STRING
            and ctx.field_max_length is not None
            and isinstance(value, int)
            and value > ctx.field_max_length
        ):
            return None, self._reject(
                "invalid_min_max", ctx, raw=raw_original,
                message=(
                    f"max_value ({value}) is greater than the type's "
                    f"max_length ({ctx.field_max_length}) for a string field"
                ),
            )
        return value, None

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        # Lowering a max bound is breaking; raising it widens the accepted range.
        return diff_numeric_bound(
            field_name, old, new,
            name=cls.name,
            raise_severity="additive",
            lower_severity="breaking",
        )
