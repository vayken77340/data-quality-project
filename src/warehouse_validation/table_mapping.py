"""Logical -> physical table-name resolver for the warehouse runners.

Bronze fidelity and reconciliation need to know which physical bronze /
silver tables correspond to the logical table named on the CLI (the same
name as the contract YAML file). The mapping lives outside the contract
so the contract stays free of warehouse plumbing.

Source: `epics/<E>/configs/warehouse.yaml`. Shape:

    tables:
      <logical_name>:
        bronze: "<catalog>.<schema>.<bronze_table>"
        silver: "<catalog>.<schema>.<silver_table>"

Missing file OR missing entry falls back to the bare-name convention
`<table>_bronze` / `<table>_silver`. Bare names are resolved by the
Trino connection's default catalog/schema, mirroring how Phase 2's
silver runner reaches its table today.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dq_core.errors import ConfigError
from dq_core.yaml_io import load_yaml_mapping


WAREHOUSE_YAML_RELPATH = Path("configs") / "warehouse.yaml"


@dataclass(frozen=True)
class TableMapping:
    """Resolved bronze/silver physical names for one logical table."""

    bronze: str
    silver: str


def load_mapping(
    epic_dir: Path,
    logical_name: str,
    connector_default_catalog: str | None = None,
    connector_default_schema: str | None = None,
) -> TableMapping:
    """Resolve the bronze/silver physical names for `logical_name`.

    Resolution rules:
      * `epics/<E>/configs/warehouse.yaml` present with an entry for
        `logical_name` -> return that entry's bronze/silver verbatim.
      * File present but no entry for `logical_name` -> bare-name
        fallback `<table>_bronze` / `<table>_silver`.
      * File missing -> bare-name fallback.

    The `connector_default_*` args exist for forward-compatibility with
    future connectors that may need explicit FQ fallbacks; Phase 3
    relies on the Trino connection's default catalog/schema to resolve
    bare names, matching the silver runner.

    Raises ConfigError when the file is malformed (bad shape) or when
    the YAML-supplied bronze and silver names collide.
    """
    _ = connector_default_catalog, connector_default_schema  # reserved

    yaml_path = epic_dir / WAREHOUSE_YAML_RELPATH
    if not yaml_path.is_file():
        return _fallback(logical_name)

    raw = load_yaml_mapping(yaml_path, what="warehouse mapping")
    tables = raw.get("tables")
    if tables is None:
        return _fallback(logical_name)
    if not isinstance(tables, dict):
        raise ConfigError(
            f"{yaml_path}: top-level 'tables' must be a mapping "
            f"(got {type(tables).__name__})"
        )

    entry = tables.get(logical_name)
    if entry is None:
        return _fallback(logical_name)
    if not isinstance(entry, dict):
        raise ConfigError(
            f"{yaml_path}: tables.{logical_name!r} must be a mapping "
            f"with 'bronze' and 'silver' keys (got {type(entry).__name__})"
        )

    bronze = entry.get("bronze")
    silver = entry.get("silver")
    missing = [k for k, v in (("bronze", bronze), ("silver", silver))
               if not isinstance(v, str) or not v]
    if missing:
        raise ConfigError(
            f"{yaml_path}: tables.{logical_name!r} is missing required "
            f"key(s): {missing}"
        )
    if bronze == silver:
        raise ConfigError(
            f"{yaml_path}: tables.{logical_name!r}.bronze and .silver "
            f"resolve to the same physical name ({bronze!r})"
        )
    return TableMapping(bronze=bronze, silver=silver)


def _fallback(logical_name: str) -> TableMapping:
    return TableMapping(
        bronze=f"{logical_name}_bronze",
        silver=f"{logical_name}_silver",
    )
