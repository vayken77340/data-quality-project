from __future__ import annotations

from data_contract.field_constraints.base import DriftChange, _BoolConstraint


class UniqueConstraint(_BoolConstraint):
    """OUI/NON-style boolean flag declaring a field is unique across rows.

    Spec cell:    OUI/NON-style boolean token (configurable via `values:` block).
    Contract output: flat `unique: true`; false is omitted from the contract.
    Drift:        added = breaking; removed = additive.
    """

    name = "unique"
    contract_key = "unique"

    CONTRACT_VALUE_SCHEMA = {"type": "boolean", "const": True}

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        old_b = bool(old)
        new_b = bool(new)
        if old_b == new_b:
            return None
        if new_b:
            return DriftChange(kind="unique_added", severity="breaking", field=field_name)
        return DriftChange(kind="unique_removed", severity="additive", field=field_name)
