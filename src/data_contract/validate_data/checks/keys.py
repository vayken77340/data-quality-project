"""Key checks: PK uniqueness + FK existence across tables.

Both checks return a Polars LazyFrame of violating rows (one row per
participating offender for PKs; one row per dangling reference for FKs).
"""

from __future__ import annotations

from data_contract.contract import Contract


def check_pk_uniqueness(frame, contract: Contract):
    """Flag every row participating in a duplicate-PK cluster.

    Returns None when the contract declares no PK fields.
    Composite PKs are handled by grouping on the full PK tuple.
    """
    pk_fields = contract.primary_key_fields()
    if not pk_fields:
        return None
    import polars as pl

    pk_cols = [f.name for f in pk_fields]

    # Find every PK tuple that appears more than once (ignoring rows where any
    # PK component is null — that's a nullability violation handled elsewhere).
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


def check_fk_existence(child_frame, fk_col: str, parent_frame, parent_pk_col: str):
    """Flag rows whose FK value is non-null but absent from the parent's PK column.

    Compares both sides as strings: contracts often declare the parent PK as
    `number` and the child FK as `string`. Type alignment is a separate
    invariant (`validate-contract`); the data-side FK check is purely about
    "does this identifier exist in the parent set."
    """
    import polars as pl

    parent_keys = [
        str(v) for v in
        parent_frame.select(pl.col(parent_pk_col).cast(pl.String, strict=False))
                    .collect().to_series().to_list()
        if v is not None
    ]
    fk_as_string = pl.col(fk_col).cast(pl.String, strict=False)
    return child_frame.filter(
        pl.col(fk_col).is_not_null() & ~fk_as_string.is_in(parent_keys)
    )
