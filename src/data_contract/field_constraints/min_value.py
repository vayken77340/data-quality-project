from __future__ import annotations

from data_contract.errors import ConfigError
from data_contract.field_constraints._typed_value import parse_typed_value
from data_contract.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_numeric_bound,
    unwrap_structured_value,
)


# NOTE: shape ~85% overlaps with max_value.py; a `_NumericBoundConstraint`
# shared base was considered + rejected in audit v7/v8. The per-side constants
# (`>=`/`>` vs `<=`/`<`, breaking-direction semantics) thread through every
# method, so the shared base would mostly carry per-side parameters rather
# than shared behaviour. Leave the duplication; revisit only if a third
# numeric-bound constraint shows up.
class MinValueConstraint(FieldConstraint):
    """An inclusive (or strict) lower bound on a field's values.

    Spec cell:    a typed value (coerced via `parse_typed_value` against the field's declared type).
    Contract output: structured `min_value: {value, strict}`; `strict: false` means `>=`, `true` means `>`.
    Drift:        value raised = breaking; value lowered = additive; strict tightened (>= -> >) = breaking;
                  strict loosened = additive.
    """

    name = "min_value"
    contract_key = "min_value"

    SPEC_PARSING_FIELDS = ()
    CONTRACT_FIELDS = ("strict",)

    CONTRACT_VALUE_SCHEMA = {
        "type": "object",
        "properties": {
            "value": {},  # coerced per field type — permissive at the JSON level
            "strict": {"type": "boolean"},
        },
        "required": ["value", "strict"],
        "additionalProperties": False,
    }

    VIOLATION_KIND = "min_value_violation"

    strict: bool

    def _configure(self) -> None:
        strict = self._contract_params.get("strict", False)
        if not isinstance(strict, bool):
            raise ConfigError(
                f"column_mapping.{self.name}.contract_params.strict must be a boolean"
            )
        self.strict = strict
        # Always emit `strict` so downstream consumers don't need to know the default.
        self._contract_params["strict"] = strict

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        value, err_msg = parse_typed_value(raw_str, ctx.field_type)
        if err_msg is not None:
            return None, self._reject(
                "invalid_min_max", ctx, raw=raw_original,
                message=f"{self.name}: {err_msg}",
            )
        return value, None

    @classmethod
    def check_data(cls, frame, field, check):
        """Flag rows whose value violates the lower bound.

        strict=False (default): violation when value < threshold.
        strict=True:            violation when value <= threshold.
        Nulls are NOT flagged here — nullability is a separate check.
        """
        import polars as pl

        threshold = check.value
        strict = bool(check.params.get("strict", False))
        col = pl.col(field.name)
        condition = (col <= threshold) if strict else (col < threshold)
        return frame.filter(col.is_not_null() & condition)

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        old_value, old_params = unwrap_structured_value(old)
        new_value, new_params = unwrap_structured_value(new)
        old_strict = bool(old_params.get("strict", False))
        new_strict = bool(new_params.get("strict", False))

        if old_value != new_value:
            # Raising a min bound is breaking; lowering widens the accepted range.
            change = diff_numeric_bound(
                field_name, old_value, new_value,
                name=cls.name,
                raise_severity="breaking",
                lower_severity="additive",
            )
            if change is not None:
                return change

        if old_strict != new_strict and old is not None and new is not None:
            # Toggling strict on a min bound: `>=` -> `>` removes the boundary
            # value from the accepted range (breaking); `>` -> `>=` widens (additive).
            if new_strict:
                return DriftChange(
                    kind="min_value_strict_tightened",
                    severity="breaking",
                    field=field_name,
                    detail={"from": old_strict, "to": new_strict},
                )
            return DriftChange(
                kind="min_value_strict_loosened",
                severity="additive",
                field=field_name,
                detail={"from": old_strict, "to": new_strict},
            )
        return None
