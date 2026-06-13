from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from data_contract.contract import FieldContract
from data_contract.type_mapping import Type, TypeRegistry, load_type_registry, _MappingEntry
from data_contract.validation.checks.core_fields import (
    check_boolean_coercion,
    check_max_length,
    check_nullable,
    check_type_coercion,
    normalize_boolean_column,
    normalize_typed_column,
)


def _frame(rows):
    return pl.LazyFrame(rows)


def _field(name, *, nullable=True, t=Type.STRING, max_length=None):
    return FieldContract(name=name, type=t, nullable=nullable, description=None, max_length=max_length)


def test_check_nullable_flags_nulls_when_not_nullable():
    frame = _frame([{"x": "a"}, {"x": None}, {"x": "b"}])
    violating = check_nullable(frame, _field("x", nullable=False))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == [None]


def test_check_nullable_returns_none_when_nullable_true():
    assert check_nullable(_frame([]), _field("x", nullable=True)) is None


def test_check_nullable_returns_none_when_nullable_unset():
    assert check_nullable(_frame([]), _field("x", nullable=None)) is None


def test_check_max_length_flags_too_long():
    frame = _frame([{"x": "abc"}, {"x": "abcdef"}, {"x": None}])
    violating = check_max_length(frame, _field("x", t=Type.STRING, max_length=4))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["abcdef"]


def test_check_max_length_skipped_for_non_string():
    assert check_max_length(_frame([]), _field("x", t=Type.INT64, max_length=5)) is None


def test_check_max_length_skipped_when_no_cap():
    assert check_max_length(_frame([]), _field("x", t=Type.STRING)) is None


def test_check_type_coercion_flags_bad_integer():
    frame = pl.LazyFrame({"x": ["1", "2", "not-a-number", "3"]})
    violating = check_type_coercion(frame, _field("x", t=Type.INT64))
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["not-a-number"]


def test_check_type_coercion_skipped_for_string():
    assert check_type_coercion(_frame([]), _field("x", t=Type.STRING)) is None


# ---------------------------------------------------------------------------
# Boolean coercion + normalization
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def boolean_registry() -> TypeRegistry:
    return TypeRegistry(entries=[
        _MappingEntry(
            canonical=Type.BOOLEAN,
            aliases=("bool", "boolean"),
            parameters=(),
            data_values={
                "true": frozenset({"true", "vrai", "oui", "1", "y", "yes"}),
                "false": frozenset({"false", "faux", "non", "0", "n", "no"}),
            },
        ),
    ])


def test_check_boolean_coercion_flags_unknown_tokens(boolean_registry):
    frame = pl.LazyFrame({"flag": ["VRAI", "faux", "maybe", "OUI", None, ""]})
    violating = check_boolean_coercion(frame, _field("flag", t=Type.BOOLEAN), boolean_registry)
    rows = violating.collect().to_dicts()
    # "maybe" doesn't match either bucket; "" strips to "" which isn't in either.
    assert [r["flag"] for r in rows] == ["maybe", ""]


def test_check_boolean_coercion_accepts_case_and_whitespace(boolean_registry):
    frame = pl.LazyFrame({"flag": [" Vrai ", "FAUX", "yEs"]})
    violating = check_boolean_coercion(frame, _field("flag", t=Type.BOOLEAN), boolean_registry)
    assert violating.collect().height == 0


def test_check_boolean_coercion_skipped_for_non_boolean(boolean_registry):
    assert check_boolean_coercion(_frame([]), _field("x", t=Type.STRING), boolean_registry) is None


def test_check_boolean_coercion_skipped_without_data_values():
    empty_reg = TypeRegistry(entries=[
        _MappingEntry(canonical=Type.BOOLEAN, aliases=("bool",), parameters=()),
    ])
    assert check_boolean_coercion(_frame([]), _field("flag", t=Type.BOOLEAN), empty_reg) is None


def test_check_type_coercion_skipped_for_boolean_when_data_values_present(boolean_registry):
    # When data_values is declared, boolean coercion is handled by check_boolean_coercion,
    # so check_type_coercion must yield to it.
    assert check_type_coercion(
        _frame([]), _field("flag", t=Type.BOOLEAN), boolean_registry
    ) is None


def test_normalize_boolean_column_maps_tokens_to_canonical(boolean_registry):
    df = pl.DataFrame({"flag": ["VRAI", "faux", "OUI", "non", None, "maybe"]})
    out = normalize_boolean_column(df, _field("flag", t=Type.BOOLEAN), boolean_registry)
    assert out["flag"].dtype == pl.Boolean
    assert out["flag"].to_list() == [True, False, True, False, None, None]


def test_normalize_boolean_column_idempotent(boolean_registry):
    df = pl.DataFrame({"flag": [True, False, None]})
    out = normalize_boolean_column(df, _field("flag", t=Type.BOOLEAN), boolean_registry)
    assert out["flag"].to_list() == [True, False, None]


def test_normalize_boolean_column_no_op_for_non_boolean(boolean_registry):
    df = pl.DataFrame({"x": [1, 2, 3]})
    out = normalize_boolean_column(df, _field("x", t=Type.INT64), boolean_registry)
    assert out["x"].to_list() == [1, 2, 3]


def test_normalize_boolean_column_no_op_when_column_missing(boolean_registry):
    df = pl.DataFrame({"other": [1, 2]})
    out = normalize_boolean_column(df, _field("flag", t=Type.BOOLEAN), boolean_registry)
    assert out.columns == ["other"]


def test_boolean_tokens_from_real_types_yaml(repo_root: Path):
    reg = load_type_registry(repo_root / "configs" / "types.yaml")
    tokens = reg.data_values_for(Type.BOOLEAN)
    assert tokens is not None
    assert {"vrai", "oui", "true"}.issubset(tokens["true"])
    assert {"faux", "non", "false"}.issubset(tokens["false"])


# ---------------------------------------------------------------------------
# check_type_coercion: new string-first behaviour (regex + parse_formats)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def date_registry() -> TypeRegistry:
    from data_contract.type_mapping import _MappingEntry  # noqa: F401 (already imported)
    return TypeRegistry(entries=[
        _MappingEntry(
            canonical=Type.DATE,
            aliases=("date",),
            parameters=(),
            parse_formats=("%Y-%m-%d", "%d/%m/%Y"),
        ),
        _MappingEntry(
            canonical=Type.TIMESTAMP,
            aliases=("timestamp",),
            parameters=(),
            parse_formats=("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"),
        ),
    ])


def test_check_type_coercion_flags_french_comma_double():
    frame = pl.LazyFrame({"x": ["1.5", "12,34", "2.0", None]})
    eager = frame.collect()
    violating = check_type_coercion(frame, _field("x", t=Type.FLOAT64), eager_df=eager)
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["12,34"]


def test_check_type_coercion_flags_whole_number_float_for_integer():
    # str(42.0) -> "42.0" which the INTEGER regex correctly rejects.
    frame = pl.LazyFrame({"x": ["42", "42.0", "-7"]})
    eager = frame.collect()
    violating = check_type_coercion(frame, _field("x", t=Type.INT64), eager_df=eager)
    rows = violating.collect().to_dicts()
    assert [r["x"] for r in rows] == ["42.0"]


def test_check_type_coercion_flags_bad_date(date_registry):
    frame = pl.LazyFrame({"d": ["2024-01-15", "15/01/2024", "yesterday", None]})
    eager = frame.collect()
    violating = check_type_coercion(
        frame, _field("d", t=Type.DATE), date_registry, eager_df=eager
    )
    rows = violating.collect().to_dicts()
    assert [r["d"] for r in rows] == ["yesterday"]


def test_check_type_coercion_flags_bad_timestamp(date_registry):
    frame = pl.LazyFrame({"t": ["2024-01-15 10:30:00", "not a timestamp"]})
    eager = frame.collect()
    violating = check_type_coercion(
        frame, _field("t", t=Type.TIMESTAMP), date_registry, eager_df=eager
    )
    rows = violating.collect().to_dicts()
    assert [r["t"] for r in rows] == ["not a timestamp"]


def test_check_type_coercion_returns_none_when_no_violations(date_registry):
    frame = pl.LazyFrame({"d": ["2024-01-15", None]})
    eager = frame.collect()
    assert check_type_coercion(
        frame, _field("d", t=Type.DATE), date_registry, eager_df=eager
    ) is None


def test_check_type_coercion_skipped_for_unknown():
    frame = pl.LazyFrame({"x": ["anything"]})
    assert check_type_coercion(frame, _field("x", t=Type.UNKNOWN), eager_df=frame.collect()) is None


def test_check_type_coercion_preserves_source_metadata_columns():
    frame = pl.LazyFrame({
        "x": ["1", "bad"],
        "__row_index__": [1, 2],
        "__source_file__": ["a.csv", "a.csv"],
    })
    eager = frame.collect()
    violating = check_type_coercion(frame, _field("x", t=Type.INT64), eager_df=eager)
    rows = violating.collect().to_dicts()
    assert rows == [{"x": "bad", "__row_index__": 2, "__source_file__": "a.csv"}]


# ---------------------------------------------------------------------------
# normalize_typed_column
# ---------------------------------------------------------------------------


def test_normalize_typed_column_int():
    df = pl.DataFrame({"x": ["1", "2", None, "bad"]})
    out = normalize_typed_column(df, _field("x", t=Type.INT64))
    assert out["x"].dtype == pl.Int64
    assert out["x"].to_list() == [1, 2, None, None]


def test_normalize_typed_column_double():
    df = pl.DataFrame({"x": ["1.5", "12,34", "2.0", None]})
    out = normalize_typed_column(df, _field("x", t=Type.FLOAT64))
    assert out["x"].dtype == pl.Float64
    assert out["x"].to_list() == [1.5, None, 2.0, None]


def test_normalize_typed_column_date(date_registry):
    from datetime import date as date_t
    df = pl.DataFrame({"d": ["2024-01-15", "15/01/2024", "bad", None]})
    out = normalize_typed_column(df, _field("d", t=Type.DATE), date_registry)
    assert out["d"].dtype == pl.Date
    assert out["d"].to_list() == [date_t(2024, 1, 15), date_t(2024, 1, 15), None, None]


def test_normalize_typed_column_timestamp(date_registry):
    from datetime import datetime as dt_t
    df = pl.DataFrame({"t": ["2024-01-15 10:30:00", "2024-01-15T11:00:00", "bad"]})
    out = normalize_typed_column(df, _field("t", t=Type.TIMESTAMP), date_registry)
    assert out["t"].dtype == pl.Datetime
    assert out["t"].to_list() == [
        dt_t(2024, 1, 15, 10, 30, 0),
        dt_t(2024, 1, 15, 11, 0, 0),
        None,
    ]


def test_normalize_typed_column_idempotent_on_already_typed():
    df = pl.DataFrame({"x": [1, 2, 3]}, schema={"x": pl.Int64})
    out = normalize_typed_column(df, _field("x", t=Type.INT64))
    assert out["x"].dtype == pl.Int64
    assert out["x"].to_list() == [1, 2, 3]


def test_normalize_typed_column_no_op_for_varchar():
    df = pl.DataFrame({"x": ["00042", "abc"]})
    out = normalize_typed_column(df, _field("x", t=Type.STRING))
    assert out["x"].dtype == pl.String
    assert out["x"].to_list() == ["00042", "abc"]


def test_normalize_typed_column_no_op_when_column_missing():
    df = pl.DataFrame({"other": [1, 2]})
    out = normalize_typed_column(df, _field("x", t=Type.INT64))
    assert out.columns == ["other"]


def test_normalize_typed_column_no_op_for_boolean_with_registry(boolean_registry):
    # When the registry has boolean tokens, normalize_boolean_column owns the
    # job and normalize_typed_column is a no-op.
    df = pl.DataFrame({"flag": ["true", "false"]})
    out = normalize_typed_column(df, _field("flag", t=Type.BOOLEAN), boolean_registry)
    assert out["flag"].dtype == pl.String  # unchanged
    assert out["flag"].to_list() == ["true", "false"]
