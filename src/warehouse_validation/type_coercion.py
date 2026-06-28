"""Canonical Type -> Trino CAST type string.

Bronze fidelity checks issue `TRY_CAST(<col> AS <trino_type>) IS NULL`
to count rows whose all-STRING bronze value can't be coerced to the
contract's declared type. The cast-target string depends on the
connector dialect; this module is the Trino half.

Lives in its own file so Phase 4+ can add `_ORACLE_CAST` /
`_PYICEBERG_CAST` siblings here without disturbing the runner.
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


def trino_cast_type(t: Type) -> str:
    """Return the Trino CAST target string for canonical type `t`.

    Raises ConfigError when `t` has no Trino mapping yet -- adding one
    is a single line in `_TRINO_CAST`.
    """
    cast = _TRINO_CAST.get(t)
    if cast is None:
        raise ConfigError(
            f"no Trino CAST type for {t!r}; supported: "
            f"{sorted(k.value for k in _TRINO_CAST)}"
        )
    return cast
