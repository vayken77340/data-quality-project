"""Warehouse-side run config.

Mirrors only what `dq_core.report_build.build_run_metadata` duck-types
against on the file side: a `.checks` attribute holding a `Gates` whose
`.specs` items map name -> `GateSpec(enabled=bool)`. Setup populates
`checks` from `sql_predicates.supported_constraint_names()` so the report's
enabled-checks list reflects the actual SQL pushdowns available.

There is no validation.yaml on the warehouse side -- the connector name
and table come from CLI args, not from a config file. The class is a
simple frozen container; loading-from-disk is deferred until a need
arises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dq_core.gates import Gates


@dataclass(frozen=True)
class WarehouseValidationConfig:
    epic: str
    connector_name: str
    table_name: str
    contract_path: Path
    output_dir: Path
    checks: Gates = field(default_factory=Gates)
