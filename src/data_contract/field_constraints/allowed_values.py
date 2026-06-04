from __future__ import annotations

from data_contract.errors import ConfigError
from data_contract.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class AllowedValuesConstraint(FieldConstraint):
    """An enumerated whitelist of permitted values for a field.

    Spec cell:    delimited string (separator configurable, default `|`).
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

    separator: str

    def _configure(self) -> None:
        sep = self._spec_parsing_params.get("separator", "|")
        if not isinstance(sep, str) or not sep:
            raise ConfigError(
                f"column_mapping.{self.name}.spec_parsing.separator must be a non-empty string"
            )
        self.separator = sep

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        values = [p for p in (s.strip() for s in raw_str.split(self.separator)) if p]
        if not values:
            return None, self._reject(
                "list_empty",
                ctx,
                raw=raw_original,
                message=(
                    f"{self.name!r} cell {raw_original!r} yields no values "
                    f"after splitting on {self.separator!r}"
                ),
            )
        return values, None

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
