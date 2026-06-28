"""Canonical Type -> SQL CAST type string, per dialect.

Bronze fidelity checks issue `TRY_CAST(<col> AS <type>) IS NULL` to
count rows whose all-STRING bronze value can't be coerced to the
contract's declared type. The cast-target string depends on the
connector dialect.

Two dialects today:
  * trino  -- BIGINT / DOUBLE / VARCHAR / ...
  * oracle -- NUMBER(38) / BINARY_DOUBLE / VARCHAR2(4000) / ...

Oracle has no native BOOLEAN; NUMBER(1) is the conventional carrier
(0/1). VARCHAR2(4000) is the max length Oracle accepts without
LOB-style storage; longer values would land in CLOB and aren't in
scope for the bronze coercibility check.

Add a new dialect by adding its dict to `_CAST_BY_DIALECT`.
"""

from __future__ import annotations

from dq_core.errors import ConfigError
from dq_core.type_mapping import Type


_TRINO_CAST: dict[Type, str] = {
    Type.INT64:     "BIGINT",
    Type.FLOAT64:   "DOUBLE",
    Type.BOOLEAN:   "BOOLEAN",
    Type.STRING:    "VARCHAR",
    Type.DATE:      "DATE",
    Type.TIMESTAMP: "TIMESTAMP",
}


_ORACLE_CAST: dict[Type, str] = {
    Type.INT64:     "NUMBER(38)",
    Type.FLOAT64:   "BINARY_DOUBLE",
    Type.BOOLEAN:   "NUMBER(1)",
    Type.STRING:    "VARCHAR2(4000)",
    Type.DATE:      "DATE",
    Type.TIMESTAMP: "TIMESTAMP(6)",
}


_CAST_BY_DIALECT: dict[str, dict[Type, str]] = {
    "trino":  _TRINO_CAST,
    "oracle": _ORACLE_CAST,
}


def cast_type(t: Type, dialect: str) -> str:
    """Return the CAST target string for canonical type `t` under
    `dialect`.

    Raises ConfigError when the dialect is unknown or when the type
    has no mapping in that dialect's table.
    """
    table = _CAST_BY_DIALECT.get(dialect)
    if table is None:
        raise ConfigError(
            f"no cast table for dialect {dialect!r}; "
            f"supported: {sorted(_CAST_BY_DIALECT)}"
        )
    cast = table.get(t)
    if cast is None:
        raise ConfigError(
            f"no {dialect} CAST type for {t!r}; supported types: "
            f"{sorted(k.value for k in table)}"
        )
    return cast
