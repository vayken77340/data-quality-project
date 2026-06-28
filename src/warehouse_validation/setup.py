"""Pre-run setup: resolve epic, instantiate the connector, optionally
load the contract.

Three entry points share one bootstrap helper:
  * `prepare_run`       -- silver / bronze / reconcile. Loads a contract
                            and builds a per-table WarehouseValidationConfig.
  * `prepare_gold_run`  -- gold assertions. Returns GoldRunSetup without
                            a contract (each rule carries its own table
                            metadata; sidecar contracts are optional).

The shared chunk (epic-name validation, output-dir resolution,
connector instantiation) lives in `_prepare_common` so the two paths
stay aligned and the per-subcommand error prefix is set once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dq_core.contract import Contract
from dq_core.epic import InvalidEpicName, validate_epic_name
from dq_core.errors import ConfigError
from dq_core.gates import Gates, GateSpec
from warehouse_validation.config import WarehouseValidationConfig
from warehouse_validation.connectors import Connector, get_connector
from warehouse_validation.sql_predicates import supported_constraint_names


DEFAULT_OUTPUT_SUBDIR = "validations_warehouse"


class RunSetupError(Exception):
    """Setup failed. The message is the full stderr line to print
    (already prefixed with the subcommand label, e.g. `validate-warehouse:`).
    Each runner's single catch maps it to exit 1.
    """


@dataclass(frozen=True)
class RunSetup:
    """Silver/bronze/reconcile setup: one contract, one table."""
    epic: str
    contract: Contract
    connector: Connector
    config: WarehouseValidationConfig


@dataclass(frozen=True)
class GoldRunSetup:
    """Gold setup: no per-table contract. Each rule carries its own
    table metadata; the runner loads sidecar contracts opportunistically.
    """
    epic: str
    epic_dir: Path
    connector: Connector
    output_dir: Path


def _prepare_common(
    *,
    cmd_label: str,
    epic: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
) -> tuple[str, Path, Path, Connector]:
    """Shared bootstrap. Returns (epic_clean, epic_dir, resolved_out,
    connector). Raises RunSetupError on any failure with messages
    prefixed by `cmd_label:`."""
    try:
        epic_clean = validate_epic_name(epic)
    except InvalidEpicName as e:
        raise RunSetupError(f"{cmd_label}: {e}") from e

    epic_dir = epic_root / epic_clean

    if output_dir is None:
        resolved_out = epic_dir / DEFAULT_OUTPUT_SUBDIR
    elif output_dir.is_absolute():
        resolved_out = output_dir
    else:
        resolved_out = epic_dir / output_dir

    try:
        connector = get_connector(connector_name)
    except ConfigError as e:
        raise RunSetupError(f"{cmd_label}: {e}") from e

    return epic_clean, epic_dir, resolved_out, connector


def prepare_run(
    *,
    epic: str,
    table: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
    cmd_label: str = "validate-warehouse",
) -> RunSetup:
    """Run every pre-validation setup step for a contract-bound run.
    Raises RunSetupError (with the full stderr line as its message)
    on any failure.

    `cmd_label` lets bronze/reconcile reuse this helper while keeping
    their own subcommand prefix in error messages. Defaults to
    `validate-warehouse` for backwards compatibility with the silver
    runner."""
    epic_clean, epic_dir, resolved_out, connector = _prepare_common(
        cmd_label=cmd_label,
        epic=epic, connector_name=connector_name,
        epic_root=epic_root, output_dir=output_dir,
    )

    contract_path = epic_dir / "contracts" / f"{table}.yaml"
    if not contract_path.is_file():
        raise RunSetupError(
            f"{cmd_label}: contract not found: {contract_path}"
        )

    try:
        contract = Contract.load(contract_path)
    except (ConfigError, OSError) as e:
        raise RunSetupError(f"{cmd_label}: {e}") from e

    checks = Gates(
        specs={
            name: GateSpec(enabled=True)
            for name in supported_constraint_names()
        },
    )

    config = WarehouseValidationConfig(
        epic=epic_clean,
        connector_name=connector_name,
        table_name=table,
        contract_path=contract_path,
        output_dir=resolved_out,
        checks=checks,
    )

    return RunSetup(epic=epic_clean, contract=contract, connector=connector, config=config)


def prepare_gold_run(
    *,
    epic: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
) -> GoldRunSetup:
    """Bootstrap a gold run: epic + connector + output_dir, but no
    contract load. The runner walks epic_dir/rules/gold/ and may
    opportunistically read sidecar contracts per rule.target_table."""
    epic_clean, epic_dir, resolved_out, connector = _prepare_common(
        cmd_label="validate-gold",
        epic=epic, connector_name=connector_name,
        epic_root=epic_root, output_dir=output_dir,
    )
    return GoldRunSetup(
        epic=epic_clean,
        epic_dir=epic_dir,
        connector=connector,
        output_dir=resolved_out,
    )
