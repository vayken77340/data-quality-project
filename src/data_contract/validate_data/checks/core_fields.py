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
from data_contract.type_mapping import Type, TypeRegistry


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


def check_type_coercion(frame, field: FieldContract, type_registry: TypeRegistry | None = None):
    """Flag rows where a non-null value cannot be coerced to the declared type.

    VARCHAR / UNKNOWN are no-ops (anything coerces to text). BOOLEAN is handled
    separately by `check_boolean_coercion` when a `data_values` token map exists
    in the registry, so that French / English / numeric tokens normalize cleanly.
    Other types attempt a non-strict cast per row; failures produce violations.
    """
    if field.type in (Type.VARCHAR, Type.UNKNOWN):
        return None
    if field.type is Type.BOOLEAN and type_registry is not None \
            and type_registry.data_values_for(Type.BOOLEAN) is not None:
        # Handled by check_boolean_coercion + normalize_boolean_column.
        return None
    import polars as pl

    target = _polars_dtype_for(field.type)
    if target is None:
        return None
    col_name = field.name
    raw = pl.col(col_name)
    casted = raw.cast(target, strict=False)
    return frame.filter(raw.is_not_null() & casted.is_null())


def boolean_token_map(type_registry: TypeRegistry) -> dict[str, frozenset[str]] | None:
    """Convenience wrapper: token map for BOOLEAN, or None if not declared."""
    return type_registry.data_values_for(Type.BOOLEAN)


def check_boolean_coercion(frame, field: FieldContract, type_registry: TypeRegistry):
    """Flag rows where a boolean field's value matches none of the declared tokens.

    Comparison is case-insensitive and whitespace-stripped, matching the form
    stored in the registry. Returns None when the field isn't boolean or when
    no `data_values` block is declared for BOOLEAN.
    """
    if field.type is not Type.BOOLEAN:
        return None
    tokens = type_registry.data_values_for(Type.BOOLEAN)
    if tokens is None:
        return None
    import polars as pl

    accepted = sorted({*tokens.get("true", set()), *tokens.get("false", set())})
    col = pl.col(field.name)
    normalized = col.cast(pl.String, strict=False).str.strip_chars().str.to_lowercase()
    return frame.filter(col.is_not_null() & ~normalized.is_in(accepted))


def normalize_boolean_column(df, field: FieldContract, type_registry: TypeRegistry):
    """Replace a boolean column's raw tokens with canonical Polars Booleans.

    Tokens listed under "true" become True, tokens under "false" become False,
    anything else (including the unmatched-token rows already flagged by
    `check_boolean_coercion`) becomes null. Operates on an eager DataFrame and
    returns a new DataFrame; idempotent — re-normalizing an already-cast
    Boolean column yields the same column.
    """
    if field.type is not Type.BOOLEAN:
        return df
    tokens = type_registry.data_values_for(Type.BOOLEAN)
    if tokens is None:
        return df
    import polars as pl

    if field.name not in df.columns:
        return df
    true_tokens = sorted(tokens.get("true", set()))
    false_tokens = sorted(tokens.get("false", set()))
    col = pl.col(field.name)
    normalized = col.cast(pl.String, strict=False).str.strip_chars().str.to_lowercase()
    expr = (
        pl.when(col.is_null())
        .then(None)
        .when(normalized.is_in(true_tokens))
        .then(pl.lit(True))
        .when(normalized.is_in(false_tokens))
        .then(pl.lit(False))
        .otherwise(None)
        .cast(pl.Boolean)
        .alias(field.name)
    )
    return df.with_columns(expr)


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
