"""Primary-key uniqueness check.

Composite PKs are handled by grouping on the full PK tuple. Rows with any
null PK component are skipped here -- that's a nullability violation
covered separately by the structural `nullable` check.
"""

from __future__ import annotations

from dq_core.table_checks.base import TableCheck


class PkUniquenessCheck(TableCheck):
    name = "pk_uniqueness"
    description = "Verify primary-key tuples are unique across rows."
    VIOLATION_KIND = "pk_not_unique"
    DIMENSION = "uniqueness"
    scope = "row"

    def check_data(
        self,
        frame,
        contract,
        *,
        data_columns=None,
        contracts_by_table=None,
        table_frames=None,
    ):
        pk_fields = contract.primary_key_fields()
        if not pk_fields:
            return None
        pk_cols = [f.silver_name for f in pk_fields]
        # Defensive: skip when any PK column is absent from the data file.
        # `column_missing` (its own check) is the place that flags the gap;
        # uniqueness has nothing to verify against a column that doesn't exist.
        if data_columns is not None and any(c not in data_columns for c in pk_cols):
            return None
        import polars as pl

        not_null_filter = None
        for col in pk_cols:
            clause = pl.col(col).is_not_null()
            not_null_filter = clause if not_null_filter is None else (not_null_filter & clause)
        dup_tuples = (
            frame.filter(not_null_filter)
            .group_by(pk_cols)
            .agg(pl.len().alias("__count__"))
            .filter(pl.col("__count__") > 1)
            .select(pk_cols)
        )
        return frame.join(dup_tuples, on=pk_cols, how="inner")
