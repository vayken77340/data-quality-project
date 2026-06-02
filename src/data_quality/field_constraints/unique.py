from __future__ import annotations

from data_quality.field_constraints.base import DriftChange, _BoolConstraint


class UniqueConstraint(_BoolConstraint):
    name = "unique"
    contract_key = "unique"

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        old_b = bool(old)
        new_b = bool(new)
        if old_b == new_b:
            return None
        if new_b:
            return DriftChange(kind="unique_added", severity="breaking", field=field_name)
        return DriftChange(kind="unique_removed", severity="additive", field=field_name)
