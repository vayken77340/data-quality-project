"""Property-based parity tests: SQL pushdown vs Polars check_data.

For each constraint, materialize the same column values into DuckDB
and into a Polars DataFrame; run the SQL predicate via DuckDB and the
Polars `check_data` impl on the frame; assert both flag the same
number of rows.

DuckDB is the in-process SQL substrate. It accepts the Trino-flavoured
SQL emitted by every constraint covered here (`<`, `>`, `IS NULL`,
`NOT IN`, `IN`-subqueries, `GROUP BY ... HAVING COUNT(*) > 1`) without
modification.

INTENTIONALLY EXCLUDED from this parity matrix:
  * format and pattern -- DuckDB exposes regexp_matches; the Trino /
    Oracle predicate uses regexp_like. Running the Trino SQL through
    DuckDB would error. A future iteration adds a DuckDB dialect to
    cover them. Document the gap here so it isn't lost.

Skipped at module level when DuckDB isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")

import polars as pl

from dq_core.contract import FieldCheck, FieldContract
from dq_core.field_constraints.allowed_values import AllowedValuesConstraint
from dq_core.field_constraints.max_value import MaxValueConstraint
from dq_core.field_constraints.min_value import MinValueConstraint
from dq_core.field_constraints.unique import UniqueConstraint
from dq_core.type_mapping import Type
from warehouse_validation.sql_predicates import to_sql_pushdown
from warehouse_validation.sql_predicates.not_null import (
    predicate as not_null_predicate,
)


def _field(name: str, type_: Type) -> FieldContract:
    return FieldContract(silver_name=name, type=type_, nullable=True, description="")


def _check(cls, value, **params) -> FieldCheck:
    return FieldCheck(
        constraint_name=cls.name,
        contract_key=cls.contract_key,
        constraint_cls=cls,
        value=value,
        params=params,
    )


def _materialize(conn, col_name, values, sql_type):
    """Drop and recreate table `t` with one column carrying `values`."""
    conn.execute("DROP TABLE IF EXISTS t")
    conn.execute(f'CREATE TABLE t ("{col_name}" {sql_type})')
    # Insert one row at a time to keep the test code dialect-agnostic; the
    # row counts here are small.
    for v in values:
        if v is None:
            conn.execute(f'INSERT INTO t VALUES (NULL)')
        elif isinstance(v, str):
            quoted = v.replace("'", "''")
            conn.execute(f"INSERT INTO t VALUES ('{quoted}')")
        else:
            conn.execute(f'INSERT INTO t VALUES ({v})')


def _polars_violations(constraint_cls, field, check, frame) -> int:
    """Run the constraint's Polars check_data and return the violation count."""
    out = constraint_cls.check_data(frame, field, check)
    return 0 if out is None else out.height


def _sql_violations(conn, sql) -> int:
    """Execute `sql` (a SELECT COUNT(*)) and return the scalar count."""
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


# -- min_value ---------------------------------------------------------------


def test_min_value_parity_non_strict(duckdb_connection):
    values = [-1, 0, 1, 2, None]
    _materialize(duckdb_connection, "col", values, "INTEGER")
    field = _field("col", Type.INT64)
    check = _check(MinValueConstraint, 0, strict=False)
    frame = pl.DataFrame({"col": values}, schema={"col": pl.Int64})

    polars_count = _polars_violations(MinValueConstraint, field, check, frame)
    pushdown = to_sql_pushdown(field, check, table_name="t")
    sql = f'SELECT COUNT(*) FROM "t" WHERE {pushdown.sql}'
    sql_count = _sql_violations(duckdb_connection, sql)

    # -1 violates; 0, 1, 2 pass; NULL passes (nullability is a separate check).
    assert polars_count == 1
    assert sql_count == polars_count


def test_min_value_parity_strict(duckdb_connection):
    values = [-1, 0, 1, 2]
    _materialize(duckdb_connection, "col", values, "INTEGER")
    field = _field("col", Type.INT64)
    check = _check(MinValueConstraint, 0, strict=True)
    frame = pl.DataFrame({"col": values}, schema={"col": pl.Int64})

    polars_count = _polars_violations(MinValueConstraint, field, check, frame)
    pushdown = to_sql_pushdown(field, check, table_name="t")
    sql = f'SELECT COUNT(*) FROM "t" WHERE {pushdown.sql}'
    sql_count = _sql_violations(duckdb_connection, sql)

    # strict: -1 AND 0 violate.
    assert polars_count == 2
    assert sql_count == polars_count


# -- max_value ---------------------------------------------------------------


def test_max_value_parity_non_strict(duckdb_connection):
    values = [9, 10, 11, 12, None]
    _materialize(duckdb_connection, "col", values, "INTEGER")
    field = _field("col", Type.INT64)
    check = _check(MaxValueConstraint, 10, strict=False)
    frame = pl.DataFrame({"col": values}, schema={"col": pl.Int64})

    polars_count = _polars_violations(MaxValueConstraint, field, check, frame)
    pushdown = to_sql_pushdown(field, check, table_name="t")
    sql = f'SELECT COUNT(*) FROM "t" WHERE {pushdown.sql}'
    sql_count = _sql_violations(duckdb_connection, sql)

    # 11, 12 violate.
    assert polars_count == 2
    assert sql_count == polars_count


# -- allowed_values ----------------------------------------------------------


def test_allowed_values_parity(duckdb_connection):
    values = ["A", "B", "C", None]
    _materialize(duckdb_connection, "col", values, "VARCHAR")
    field = _field("col", Type.STRING)
    check = _check(AllowedValuesConstraint, ["A", "B"])
    frame = pl.DataFrame({"col": values}, schema={"col": pl.String})

    polars_count = _polars_violations(AllowedValuesConstraint, field, check, frame)
    pushdown = to_sql_pushdown(field, check, table_name="t")
    sql = f'SELECT COUNT(*) FROM "t" WHERE {pushdown.sql}'
    sql_count = _sql_violations(duckdb_connection, sql)

    # "C" violates; NULL passes (whitelist check excludes nulls explicitly).
    assert polars_count == 1
    assert sql_count == polars_count


# -- not_null ----------------------------------------------------------------


def test_not_null_parity(duckdb_connection):
    values = [1, None, 3, None]
    _materialize(duckdb_connection, "col", values, "INTEGER")
    field = _field("col", Type.INT64)
    frame = pl.DataFrame({"col": values}, schema={"col": pl.Int64})

    # Polars side: rows where col IS NULL.
    polars_count = frame.filter(pl.col("col").is_null()).height
    pushdown = not_null_predicate(field, table_name="t")
    sql = f'SELECT COUNT(*) FROM "t" WHERE {pushdown.sql}'
    sql_count = _sql_violations(duckdb_connection, sql)

    assert polars_count == 2
    assert sql_count == polars_count


# -- unique ------------------------------------------------------------------


def test_unique_parity(duckdb_connection):
    # 2 appears twice, 3 appears twice -> 4 violating rows total (Polars
    # joins back to the duplicate keys, returning each row in a collision
    # group; the SQL IN-subquery is the same row-level semantics).
    values = [1, 2, 2, 3, 3, None]
    _materialize(duckdb_connection, "col", values, "INTEGER")
    field = _field("col", Type.INT64)
    check = _check(UniqueConstraint, True)
    frame = pl.DataFrame({"col": values}, schema={"col": pl.Int64})

    polars_count = _polars_violations(UniqueConstraint, field, check, frame)
    pushdown = to_sql_pushdown(field, check, table_name="t")
    # unique returns kind="count_query" -- execute verbatim.
    sql_count = _sql_violations(duckdb_connection, pushdown.sql)

    assert polars_count == 4
    assert sql_count == polars_count
