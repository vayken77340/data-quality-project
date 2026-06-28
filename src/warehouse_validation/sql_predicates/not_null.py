"""SQL pushdown for nullability: rows where a non-null field is NULL.

Nullability isn't a registered constraint class in dq_core -- it lives
on `FieldContract.nullable` and the file-side validator handles it
structurally (data_contract/validation/phases.py emits the
`nullable_violation` kind). This helper produces the equivalent SQL so
the silver runner can do the same pushdown without going through the
constraint dispatch table.

The runner calls `predicate(field, table_name)` directly for every
field with `nullable is False`. Not registered in `_DISPATCH`.
"""

from __future__ import annotations

from warehouse_validation.sql_predicates._util import PushdownSQL


def predicate(field, table_name, dialect: str = "trino") -> PushdownSQL:
    """Return the WHERE fragment that selects null rows in `field`.

    Used by the silver runner's nullable pass for fields whose
    contract declares `nullable: false`. `table_name` is unused; kept
    for signature symmetry with the dispatch predicates.
    """
    return PushdownSQL(kind="where", sql=f'"{field.name}" IS NULL')
