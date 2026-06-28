"""Pre-run setup: resolve epic, load the contract, instantiate the
connector, build the WarehouseValidationConfig.

Mirrors the data_contract.validation.setup.prepare_run shape (epic
validation, contract load, RunSetupError -> exit 1 path) but stripped to
what the warehouse runner needs. There is no validation.yaml on the
warehouse side -- the gates come from the registered SQL pushdowns.
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
    """Setup failed. The message is the full stderr line to print (already
    prefixed with `validate-warehouse:`). Runner's single catch maps it
    to exit 1.
    """


@dataclass(frozen=True)
class RunSetup:
    epic: str
    contract: Contract
    connector: Connector
    config: WarehouseValidationConfig


def prepare_run(
    *,
    epic: str,
    table: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
) -> RunSetup:
    """Run every pre-validation setup step. Raises RunSetupError (with the
    full stderr line as its message) on any failure."""
    try:
        epic_clean = validate_epic_name(epic)
    except InvalidEpicName as e:
        raise RunSetupError(f"validate-warehouse: {e}") from e

    epic_dir = epic_root / epic_clean
    contract_path = epic_dir / "contracts" / f"{table}.yaml"
    if not contract_path.is_file():
        raise RunSetupError(
            f"validate-warehouse: contract not found: {contract_path}"
        )

    try:
        contract = Contract.load(contract_path)
    except (ConfigError, OSError) as e:
        raise RunSetupError(f"validate-warehouse: {e}") from e

    if output_dir is None:
        resolved_out = epic_dir / DEFAULT_OUTPUT_SUBDIR
    elif output_dir.is_absolute():
        resolved_out = output_dir
    else:
        resolved_out = epic_dir / output_dir

    try:
        connector = get_connector(connector_name)
    except ConfigError as e:
        raise RunSetupError(f"validate-warehouse: {e}") from e

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
