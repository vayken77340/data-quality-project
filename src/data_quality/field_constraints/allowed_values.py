from __future__ import annotations

from data_quality.errors import ConfigError
from data_quality.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class AllowedValuesConstraint(FieldConstraint):
    """The `list` YAML key: an allowed-values whitelist for a field."""

    name = "list"
    contract_key = "list"

    def __init__(self, column, separator: str) -> None:
        super().__init__(column)
        self.separator = separator

    @classmethod
    def _extra_init_args(cls, raw):
        separator = raw.get("separator", "|")
        if not isinstance(separator, str) or not separator:
            raise ConfigError(f"column_mapping.{cls.name}.separator must be a non-empty string")
        return {"separator": separator}

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
            added_kind="list_added", added_severity="breaking",
            removed_kind="list_removed", removed_severity="additive",
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
            return DriftChange(kind="list_values_removed", severity="breaking", field=field_name, detail=detail)
        return DriftChange(kind="list_values_added", severity="additive", field=field_name, detail={"added": added})
