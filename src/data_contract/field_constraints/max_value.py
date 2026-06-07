from __future__ import annotations

from data_contract.errors import ConfigError
from data_contract.field_constraints._typed_value import parse_typed_value
from data_contract.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_numeric_bound,
    unwrap_structured_value,
)
from data_contract.type_mapping import Type


class MaxValueConstraint(FieldConstraint):
    """An inclusive (or strict) upper bound on a field's values.

    Spec cell:    a typed value (coerced via `parse_typed_value`); for string fields, treated as a length cap.
    Contract output: structured `max_value: {value, strict}`; `strict: false` means `<=`, `true` means `<`.
    Drift:        value lowered = breaking; value raised = additive; strict tightened (<= -> <) = breaking;
                  strict loosened = additive.
    """

    name = "max_value"
    contract_key = "max_value"

    SPEC_PARSING_FIELDS = ()
    CONTRACT_FIELDS = ("strict",)

    CONTRACT_VALUE_SCHEMA = {
        "type": "object",
        "properties": {
            "value": {},
            "strict": {"type": "boolean"},
        },
        "required": ["value", "strict"],
        "additionalProperties": False,
    }

    VIOLATION_KIND = "max_value_violation"

    strict: bool

    def _configure(self) -> None:
        strict = self._contract_params.get("strict", False)
        if not isinstance(strict, bool):
            raise ConfigError(
                f"column_mapping.{self.name}.contract_params.strict must be a boolean"
            )
        self.strict = strict
        self._contract_params["strict"] = strict

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
            ctx.field_type is Type.VARCHAR
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
    def check_data(cls, frame, field, check):
        """Flag rows whose value violates the upper bound.

        strict=False (default): violation when value > threshold.
        strict=True:            violation when value >= threshold.
        Nulls are NOT flagged here.
        """
        import polars as pl

        threshold = check.value
        strict = bool(check.params.get("strict", False))
        col = pl.col(field.name)
        condition = (col >= threshold) if strict else (col > threshold)
        return frame.filter(col.is_not_null() & condition)

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        old_value, old_params = unwrap_structured_value(old)
        new_value, new_params = unwrap_structured_value(new)
        old_strict = bool(old_params.get("strict", False))
        new_strict = bool(new_params.get("strict", False))

        if old_value != new_value:
            # Lowering a max bound is breaking; raising widens the accepted range.
            change = diff_numeric_bound(
                field_name, old_value, new_value,
                name=cls.name,
                raise_severity="additive",
                lower_severity="breaking",
            )
            if change is not None:
                return change

        if old_strict != new_strict and old is not None and new is not None:
            # `<=` -> `<` removes the boundary value (breaking); `<` -> `<=` widens.
            if new_strict:
                return DriftChange(
                    kind="max_value_strict_tightened",
                    severity="breaking",
                    field=field_name,
                    detail={"from": old_strict, "to": new_strict},
                )
            return DriftChange(
                kind="max_value_strict_loosened",
                severity="additive",
                field=field_name,
                detail={"from": old_strict, "to": new_strict},
            )
        return None
