"""Core field checks: nullable, max_length, type coercion, precision/scale.

These cover the hard-coded contract attributes that aren't registered as
constraints. Each function returns a Polars LazyFrame of violating rows, or
None when the check doesn't apply to the given field.

Polars is lazy-imported inside each function so this module is safe to import
without the validate-data extras.
"""

from __future__ import annotations

from typing import Any

from data_contract.contract import FieldContract
from data_contract.type_mapping import Type


def check_nullable(frame, field: FieldContract):
    """Flag rows where a non-nullable field has a null value.

    Returns None if `field.nullable` is not False (i.e. nulls allowed).
    """
    if field.nullable is not False:
        return None
    import polars as pl

    return frame.filter(pl.col(field.name).is_null())


def check_max_length(frame, field: FieldContract):
    """Flag rows where a varchar field's length exceeds max_length.

    Only applicable to VARCHAR fields with `max_length` set.
    """
    if field.type is not Type.VARCHAR or field.max_length is None:
        return None
    import polars as pl

    col = pl.col(field.name).cast(pl.String, strict=False)
    return frame.filter(col.is_not_null() & (col.str.len_chars() > field.max_length))


def check_type_coercion(frame, field: FieldContract):
    """Flag rows where a non-null value cannot be coerced to the declared type.

    VARCHAR / UNKNOWN are no-ops (anything coerces to text). Other types
    attempt a strict cast per row; failures produce violations.
    """
    if field.type in (Type.VARCHAR, Type.UNKNOWN):
        return None
    import polars as pl

    target = _polars_dtype_for(field.type)
    if target is None:
        return None
    col_name = field.name
    raw = pl.col(col_name)
    casted = raw.cast(target, strict=False)
    return frame.filter(raw.is_not_null() & casted.is_null())


def _polars_dtype_for(t: Type):
    import polars as pl

    return {
        Type.INTEGER:   pl.Int64,
        Type.DOUBLE:    pl.Float64,
        Type.FLOAT:     pl.Float32,
        Type.BOOLEAN:   pl.Boolean,
        Type.DATE:      pl.Date,
        Type.TIMESTAMP: pl.Datetime,
    }.get(t)
