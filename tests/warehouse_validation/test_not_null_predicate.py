"""Unit tests for the not_null pushdown helper.

not_null isn't in the dispatch table (nullability lives on
FieldContract.nullable, not as a registered constraint class), so it
gets its own test file rather than going through test_sql_predicates.
"""

from __future__ import annotations

from dq_core.contract import FieldContract
from dq_core.type_mapping import Type
from warehouse_validation.sql_predicates import not_null
from warehouse_validation.sql_predicates._util import PushdownSQL


def _field(name: str) -> FieldContract:
    return FieldContract(
        silver_name=name, type=Type.INT64, nullable=False, description="",
    )


def test_predicate_returns_where_fragment():
    p = not_null.predicate(_field("pk"), table_name="t")
    assert p == PushdownSQL(kind="where", sql='"pk" IS NULL')


def test_predicate_quotes_field_name():
    # A field name containing characters that aren't legal identifiers
    # should still be wrapped in double quotes (the contract model
    # rejects exotic field names at parse time, but the helper makes no
    # assumptions).
    p = not_null.predicate(_field("snake_case_col"), table_name="t")
    assert p.sql == '"snake_case_col" IS NULL'


def test_predicate_signature_accepts_dialect():
    # Dialect arg exists for signature symmetry with the dispatch
    # predicates; not_null SQL is portable across Trino / Oracle / DuckDB.
    p = not_null.predicate(_field("x"), table_name="t", dialect="oracle")
    assert p == PushdownSQL(kind="where", sql='"x" IS NULL')
