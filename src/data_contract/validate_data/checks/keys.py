"""Key checks: PK uniqueness + FK existence across tables.

Both checks return a Polars LazyFrame of violating rows (one row per
participating offender for PKs; one row per dangling reference for FKs).
"""

from __future__ import annotations

from typing import Any

from data_contract.contract import Contract


def _canonical_str(value: Any) -> str | None:
    """Render a Python value as the canonical string the parser would produce.

    Used for FK comparisons where contracts often declare parent PK and child
    FK as different canonical types -- the comparison must compute the same
    string form on both sides.

    Conventions:
      - bool                -> "true" / "false" (lowercase, matching the
                               BOOLEAN data_values literal convention).
      - whole-number float  -> "1" not "1.0" (matches the Excel parser's
                               `_to_string` downcast, so a parent Float64(1.0)
                               and a child String "1" both canonicalise to "1").
      - everything else     -> `str(value)`.

    Note `bool` must be checked BEFORE `float` because `bool` is a subclass
    of `int` in Python (but not `float`); the explicit isinstance keeps the
    ordering safe either way.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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

    Both sides are canonicalised to strings via `_canonical_str` (whole-number
    floats stringify as "1" not "1.0"). This keeps the FK check consistent
    when contracts declare parent PK and child FK as different canonical
    types -- a common case where the parent PK is `double` (Float64 after
    normalisation) but the child FK is `varchar` (still String).
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
