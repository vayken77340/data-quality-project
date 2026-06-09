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
from data_contract.validate_data.checks.value_parsers import get_parser


def check_nullable(frame, field: FieldContract):
    """Flag rows where a non-nullable field has a null value.

    Returns None if `field.nullable` is not False (i.e. nulls allowed).
    """
    if field.nullable is not False:
        return None
    import polars as pl

    return frame.filter(pl.col(field.name).is_null())


def check_max_length(
    frame, field: FieldContract, type_registry: TypeRegistry | None = None,
):
    """Flag rows where a string field's length exceeds max_length.

    Only applicable to STRING fields with `max_length` set. The target's
    `length_unit` setting (bytes vs characters) controls the measurement
    semantics: Oracle's VARCHAR2(n BYTE) and Postgres's VARCHAR(n CHAR) behave
    differently for multi-byte UTF-8 strings.
    """
    if field.type is not Type.STRING or field.max_length is None:
        return None
    import polars as pl

    length_unit = (
        type_registry.length_unit_for(Type.STRING) if type_registry else "characters"
    )
    col = pl.col(field.name).cast(pl.String, strict=False)
    length = col.str.len_bytes() if length_unit == "bytes" else col.str.len_chars()
    return frame.filter(col.is_not_null() & (length > field.max_length))


def check_type_coercion(
    frame,
    field: FieldContract,
    type_registry: TypeRegistry | None = None,
    *,
    eager_df=None,
):
    """Flag rows whose raw-string value cannot be parsed into the declared type.

    Replaces the previous Polars `cast`-based approach with a per-row call into
    `value_parsers.get_parser`. Reason: Polars can't express "try strptime
    against an ordered list of formats" cleanly, and the runner already
    materialises the frame to an eager DataFrame before this check runs.

    VARCHAR / UNKNOWN are no-ops (anything coerces to text). BOOLEAN is handled
    separately by `check_boolean_coercion` when a `data_values` token map exists
    in the registry. Other types dispatch into `value_parsers`.

    `eager_df` is the runner's already-collected DataFrame. When omitted (e.g.
    in unit tests that pass a LazyFrame directly), the function collects the
    frame itself.
    """
    if field.type in (Type.STRING, Type.UNKNOWN):
        return None
    if field.type is Type.BOOLEAN and type_registry is not None \
            and type_registry.data_values_for(Type.BOOLEAN) is not None:
        # Handled by check_boolean_coercion + normalize_boolean_column.
        return None
    parser = get_parser(field.type)
    if parser is None:
        return None

    import polars as pl

    kwargs = _parser_kwargs(field, type_registry)
    df = eager_df if eager_df is not None else frame.collect()
    if field.name not in df.columns:
        return None

    violating_rows: list[dict[str, Any]] = []
    schema = df.schema
    for row in df.iter_rows(named=True):
        raw = row[field.name]
        if raw is None:
            continue
        # Defensive: if a column has already been normalised to a non-string
        # dtype (e.g. boolean), do not re-run the string parser on it.
        if not isinstance(raw, str):
            continue
        _, err = parser(raw, **kwargs)
        if err is not None:
            violating_rows.append(row)
    if not violating_rows:
        return None
    return pl.DataFrame(violating_rows, schema=schema).lazy()


def normalize_typed_column(
    df,
    field: FieldContract,
    type_registry: TypeRegistry | None = None,
):
    """Replace a column's raw strings with parsed canonical values.

    Cells that parse cleanly become the canonical Python value (int, float,
    date, datetime). Cells that fail (already flagged by check_type_coercion)
    become null. Mirrors `normalize_boolean_column`: returns a new DataFrame
    with the column re-typed to the target Polars dtype.

    Idempotent: re-normalising a column whose dtype is not String (e.g. already
    Int64) returns the DataFrame unchanged.
    """
    if field.type in (Type.STRING, Type.UNKNOWN):
        return df
    if field.type is Type.BOOLEAN and type_registry is not None \
            and type_registry.data_values_for(Type.BOOLEAN) is not None:
        return df  # normalize_boolean_column owns this path
    parser = get_parser(field.type)
    if parser is None:
        return df

    import polars as pl

    if field.name not in df.columns:
        return df
    if df.schema[field.name] != pl.String:
        return df
    target_dtype = _polars_dtype_for(
        field.type, precision=field.precision, scale=field.scale,
    )
    if target_dtype is None:
        return df

    kwargs = _parser_kwargs(field, type_registry)
    parsed: list[Any] = []
    for raw in df[field.name].to_list():
        if raw is None:
            parsed.append(None)
            continue
        value, _ = parser(raw, **kwargs)
        parsed.append(value)
    return df.with_columns(pl.Series(field.name, parsed, dtype=target_dtype))


def _parser_kwargs(field: FieldContract, type_registry: TypeRegistry | None) -> dict[str, Any]:
    """Resolve the type-specific kwargs each parser expects."""
    t = field.type
    if t in (Type.DATE, Type.TIMESTAMP, Type.TIMESTAMP_TZ):
        formats = type_registry.parse_formats_for(t) if type_registry else ()
        return {"formats": formats}
    if t is Type.BOOLEAN:
        tokens = type_registry.data_values_for(Type.BOOLEAN) if type_registry else None
        return {"tokens": tokens or {"true": frozenset(), "false": frozenset()}}
    if t is Type.STRING:
        unit = type_registry.length_unit_for(Type.STRING) if type_registry else "characters"
        return {"max_length": field.max_length, "length_unit": unit}
    if t in (Type.INT32, Type.INT64, Type.FLOAT32, Type.FLOAT64):
        bounds = type_registry.bounds_for(t) if type_registry else None
        return {"bounds": bounds}
    if t is Type.DECIMAL:
        bounds = type_registry.bounds_for(Type.DECIMAL) if type_registry else None
        return {"precision": field.precision, "scale": field.scale, "bounds": bounds}
    return {}


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


def _polars_dtype_for(t: Type, *, precision: int | None = None, scale: int | None = None):
    import polars as pl

    if t is Type.DECIMAL and precision is not None and scale is not None:
        return pl.Decimal(precision=precision, scale=scale)
    if t is Type.TIMESTAMP_TZ:
        return pl.Datetime(time_zone="UTC")
    return {
        Type.INT32:     pl.Int32,
        Type.INT64:     pl.Int64,
        Type.FLOAT32:   pl.Float32,
        Type.FLOAT64:   pl.Float64,
        Type.BOOLEAN:   pl.Boolean,
        Type.DATE:      pl.Date,
        Type.TIMESTAMP: pl.Datetime,
        Type.BINARY:    pl.Binary,
    }.get(t)
