"""Foreign-key existence check.

Cross-table check: each non-null FK value in a child table must exist as
a PK value in the parent table. Both sides are canonicalised to strings
so a parent PK declared as Float64(1.0) and a child FK declared as
String "1" still match.

Sets `requires_cross_table = True` so the runner schedules it in the
post-all-tables phase, after every contract's frame is loaded.
"""

from __future__ import annotations

from typing import Any

from data_contract.table_checks.base import TableCheck
from data_contract.violations import Violation


def _canonical_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class FkExistenceCheck(TableCheck):
    name = "fk_existence"
    description = "Verify foreign-key values exist in the referenced parent table."
    VIOLATION_KIND = "fk_not_found"
    DIMENSION = "consistency"
    scope = "row"
    requires_cross_table = True

    def check_data(
        self,
        frame,
        contract,
        *,
        data_columns=None,
        contracts_by_table=None,
        table_frames=None,
    ):
        """Returns a list[Violation] directly because FK violations are
        synthesised per (child FK field, missing key); the runner's
        cross-table phase routes each child FK field through here once.

        Actually -- this is a single-FK-field check. The runner's
        post-all-tables loop iterates the child table's foreign_key_fields()
        and calls a thin wrapper. To preserve the old behavior 1:1, this
        TableCheck delegates to `run_for_field` which is what the runner
        calls per FK field.
        """
        # The frame-level check is a no-op: FK is per-field, not per-table.
        # The runner iterates FK fields and calls `run_for_field` directly.
        return None

    @staticmethod
    def run_for_field(child_frame, fk_col: str, parent_frame, parent_pk_col: str):
        """Return a Polars LazyFrame of rows whose FK value is non-null but
        absent from the parent's PK column. Returns None when nothing dangles.
        """
        import polars as pl

        parent_keys = {
            _canonical_str(v)
            for v in parent_frame.select(parent_pk_col).collect().to_series().to_list()
            if v is not None
        }
        child_collected = child_frame.collect()
        fk_values = child_collected[fk_col].to_list()
        mask = [
            v is not None and _canonical_str(v) not in parent_keys
            for v in fk_values
        ]
        if not any(mask):
            return None
        return child_collected.filter(pl.Series(mask)).lazy()
