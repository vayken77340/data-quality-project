from __future__ import annotations

from dq_core.column_ref import normalise_separators, split_on_separators
from dq_core.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class AllowedValuesConstraint(FieldConstraint):
    """An enumerated whitelist of permitted values for a field.

    Spec cell:    delimited string (separator configurable, default `|`;
                  may be a scalar string or a list of strings).
    Contract output: flat list `allowed_values: [...]`.
    Drift:        values added = additive; values removed = breaking.
    """

    name = "allowed_values"
    contract_key = "allowed_values"

    SPEC_PARSING_FIELDS = ("separator",)
    CONTRACT_FIELDS = ()

    CONTRACT_VALUE_SCHEMA = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "uniqueItems": True,
    }

    VIOLATION_KIND = "allowed_values_violation"

    separators: tuple[str, ...]

    def _configure(self) -> None:
        self.separators = normalise_separators(
            self._spec_parsing_params.get("separator"),
            prefix=f"column_mapping.{self.name}", key="spec_parsing",
        )

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        values = split_on_separators(raw_str, self.separators)
        if not values:
            sep_msg = (
                repr(self.separators[0]) if len(self.separators) == 1
                else "any of " + ", ".join(repr(s) for s in self.separators)
            )
            return None, self._reject(
                "list_empty",
                ctx,
                raw=raw_original,
                message=(
                    f"{self.name!r} cell {raw_original!r} yields no values "
                    f"after splitting on {sep_msg}"
                ),
            )
        return values, None

    @classmethod
    def check_data(cls, frame, field, check):
        """Flag rows whose value is non-null AND not in the allowed set.

        Nulls are NOT flagged here — nullability is enforced separately by the
        core-fields nullable check.
        """
        import polars as pl

        allowed = list(check.value or [])
        col = pl.col(field.silver_name).cast(pl.String, strict=False)
        return frame.filter(col.is_not_null() & ~col.is_in(allowed))

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        edge = diff_added_or_removed(
            field_name, old, new,
            added_kind="allowed_values_added", added_severity="breaking",
            removed_kind="allowed_values_removed", removed_severity="additive",
        )
        if edge is not None:
            return edge
        if old is None and new is None:
            return None
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        if not added and not removed:
            return None
        if removed:
            detail = {"removed": removed}
            if added:
                detail["added"] = added
            return DriftChange(kind="allowed_values_removed_values", severity="breaking", field=field_name, detail=detail)
        return DriftChange(kind="allowed_values_added_values", severity="additive", field=field_name, detail={"added": added})
