"""Pre-validation setup.

`run_validate_data` used to inline six sequential try/except blocks that all
followed the same shape: parse a config object, on `ConfigError | OSError`
print `validate-data: <prefix><error>` to stderr and return 1. This module
collapses those onto one `prepare_run` function that builds the full
`RunSetup` dataclass and raises `RunSetupError` on any failure; the runner's
single top-level `try/except RunSetupError` reduces what used to be five
nested try blocks plus an `_AbortRun` exception-as-control-flow seam down
to one catch.

Also owns the path / contract / table-config resolution helpers that are
all "before any phase runs" work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dq_core.contract import Contract
from dq_core.epic import InvalidEpicName, validate_epic_name
from dq_core.yaml_io import load_yaml
from dq_core.errors import ConfigError
from dq_core.settings import Settings, SettingsError, load_settings
from dq_core.targets import load_target_config, resolve_target_path
from dq_core.type_mapping import TypeRegistry, load_type_registry
from data_contract.validation.config import (
    TableValidationConfig,
    ValidationConfig,
)


DEFAULT_OUTPUT_SUBDIR = "validations"


class RunSetupError(Exception):
    """Setup failed. The exception message is the full stderr line to print
    (already prefixed with `validate-data:`). One catch in
    `runner.run_validate_data` translates it to exit code 1.
    """


@dataclass(frozen=True)
class RunSetup:
    """Frozen bundle of everything `run_validate_data` needs after setup."""
    epic: str
    epic_dir: Path
    input_dir: Path
    out_dir: Path
    config: ValidationConfig
    settings: Settings
    type_registry: TypeRegistry
    target_config: Any                                  # TargetConfig from targets.py
    contracts_by_table: dict[str, Contract]


def prepare_run(
    *,
    epic: str,
    input_dir: Path | None,
    output_dir: Path | None,
    epic_root: Path,
    types_path: Path,
) -> RunSetup:
    """Run every pre-validation setup step. Raises `RunSetupError` (with the
    full stderr line as its message) on any failure."""
    try:
        epic = validate_epic_name(epic)
    except InvalidEpicName as e:
        raise RunSetupError(f"validate-data: {e}") from e

    epic_dir = epic_root / epic
    configs_dir = epic_dir / "configs"
    validation_yaml = configs_dir / "validation.yaml"
    parsers_yaml_path = types_path.parent / "parsers.yaml"
    resolved_input = _resolve_epic_path(input_dir, epic_dir, default_subdir=None)
    resolved_out = _resolve_epic_path(output_dir, epic_dir, default_subdir=DEFAULT_OUTPUT_SUBDIR)

    config = _try_setup(
        "",
        lambda: ValidationConfig.from_yaml(validation_yaml, parsers_yaml_path),
    )
    settings = _try_setup("", load_settings)
    type_registry = _try_setup(
        f"failed to load type registry {types_path}: ",
        lambda: load_type_registry(types_path),
    )

    contracts_dir = config.contracts_folder if config.contracts_folder else (epic_dir / "contracts")
    contracts_by_table = _try_setup(
        f"failed to load contracts under {contracts_dir}: ",
        lambda: _load_contracts(contracts_dir),
    )

    target_name = _try_setup("", lambda: _resolve_target_name(contracts_by_table))

    # NOTE: shape similar to generation/pipeline.py's `_overlay_target_or_warn`
    # but with RunSetupError (hard fail) policy; the generator's site uses
    # WARN-and-continue. Parameterising was considered + rejected in audit
    # v7/v8 -- the different error policies dominate the shared shape.
    def _load_target():
        target_path = resolve_target_path(
            target_name, repo_root=types_path.parent.parent, epic_dir=epic_dir,
        )
        return load_target_config(target_path)
    target_config = _try_setup("", _load_target)
    type_registry = type_registry.with_target(target_config)

    return RunSetup(
        epic=epic, epic_dir=epic_dir,
        input_dir=resolved_input, out_dir=resolved_out,
        config=config, settings=settings,
        type_registry=type_registry, target_config=target_config,
        contracts_by_table=contracts_by_table,
    )


def _try_setup(label: str, fn):
    """Run `fn()`; on `ConfigError | OSError | SettingsError`, raise
    `RunSetupError` carrying the user-facing stderr line. `label` may end with
    ": " for "verb the noun: <error>" framings."""
    try:
        return fn()
    except (ConfigError, OSError, SettingsError) as e:
        raise RunSetupError(f"validate-data: {label}{e}") from e


def resolve_table_configs(
    config: ValidationConfig,
    contracts_by_table: dict[str, Contract],
    table_filter: str | None,
) -> dict[str, TableValidationConfig]:
    """Determine which tables to validate.

    - `tables:` omitted -> every contract validated using the defaults block.
    - `tables:` set -> only those entries; each must have a matching contract.
    - `--table <name>` -> restrict further to that single table.
    """
    if config.is_filtered():
        missing = [t for t in config.tables if t not in contracts_by_table]
        if missing:
            raise ConfigError(
                f"tables block lists {missing} but no matching contract YAMLs found"
            )
        resolved = dict(config.tables)
    else:
        resolved = {t: config.build_table_entry(t) for t in contracts_by_table}

    if table_filter is not None:
        if table_filter not in resolved:
            return {}
        return {table_filter: resolved[table_filter]}
    return resolved


def _resolve_epic_path(
    supplied: Path | None, epic_dir: Path, *, default_subdir: str | None,
) -> Path:
    if supplied is None:
        return epic_dir / default_subdir if default_subdir else epic_dir
    if supplied.is_absolute():
        return supplied
    return epic_dir / supplied


def _load_contracts(contracts_dir: Path) -> dict[str, Contract]:
    if not contracts_dir.is_dir():
        raise ConfigError(f"contracts directory not found: {contracts_dir}")
    out: dict[str, Contract] = {}
    for yaml_path in sorted(contracts_dir.glob("*.yaml")):
        if yaml_path.name == "joins.yaml":
            continue
        contract = Contract.from_dict(load_yaml(yaml_path))
        out[contract.table] = contract
    return out


def _resolve_target_name(contracts_by_table: dict[str, Contract]) -> str:
    """Pick the validation target from the loaded contracts.

    Every contract carries its own `target:` (stamped at generation from the
    epic version config). All contracts loaded in a single run must agree --
    mixing oracle and postgres contracts in one validation pass would silently
    apply the wrong type overlay to half of them.
    """
    if not contracts_by_table:
        raise ConfigError(
            "no contracts loaded; cannot resolve target. Generate at least "
            "one contract under contracts_folder before running validate-data."
        )
    targets = {c.target for c in contracts_by_table.values()}
    if "" in targets and len(targets) > 1:
        non_empty = sorted(t for t in targets if t)
        raise ConfigError(
            "some contracts carry a 'target:' field and some do not "
            f"(declared: {non_empty}). Regenerate every contract so they "
            "agree on a single target."
        )
    if len(targets) > 1:
        raise ConfigError(
            f"contracts in this run declare multiple targets ({sorted(targets)}); "
            "validate-data can only apply one target overlay per run. "
            "Split the contracts across separate validation runs."
        )
    target = next(iter(targets))
    if not target:
        raise ConfigError(
            "no 'target:' declared in the loaded contracts. Add `target: <name>` "
            "to the epic version config (e.g. configs/contracts/v1.0.yaml) and regenerate."
        )
    return target
