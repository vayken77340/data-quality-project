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

    VIOLATION_KIND = "unique_violation"

    @classmethod
    def check_data(cls, frame, field, check):
        """Flag every row that shares its value with another row.

        Nulls are NOT flagged here — uniqueness is about non-null collisions;
        nullability is handled separately.
        """
        import polars as pl

        col = pl.col(field.name)
        dup_keys = (
            frame.filter(col.is_not_null())
            .group_by(field.name)
            .agg(pl.len().alias("__count__"))
            .filter(pl.col("__count__") > 1)
            .select(field.name)
        )
        return frame.join(dup_keys, on=field.name, how="inner")

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        old_b = bool(old)
        new_b = bool(new)
        if old_b == new_b:
            return None
        if new_b:
            return DriftChange(kind="unique_added", severity="breaking", field=field_name)
        return DriftChange(kind="unique_removed", severity="additive", field=field_name)
