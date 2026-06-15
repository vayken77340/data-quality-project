"""Validation runner.

Orchestrates:
1. Load `validation.yaml` + per-parser YAMLs.
2. Load every contract YAML under the epic's `contracts/` directory.
3. For each table:
   - Glob the input directory for files matching `file_pattern`.
   - Instantiate the parser with the effective params, read into a unified LazyFrame.
   - Apply `field_mapping` to rename data columns to contract field names.
   - Run structural / core-field / per-constraint / table checks via `phases.PHASES`.
4. After all tables loaded, run cross-table FK existence checks.
5. Build per-table profile, score, rejected_rows, by_check, metrics.
6. Build run metadata and dispatch to the four report writers.

Polars is lazy-imported -- this module is import-safe even without the
validate-data extras.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from data_contract._util import now_iso_z
from data_contract.contract import Contract
from data_contract.core.epic import InvalidEpicName, validate_epic_name
from data_contract.core.yaml_io import load_yaml
from data_contract.errors import ConfigError
from data_contract.settings import Settings, SettingsError, load_settings
from data_contract.targets import load_target_config, resolve_target_path
from data_contract.type_mapping import TypeRegistry, load_type_registry
from data_contract.validation.config import (
    TableValidationConfig,
    ValidationConfig,
)
from data_contract.validation.emit import emit_from_lazy
from data_contract.validation.fk_pass import run_cross_table_fk
from data_contract.validation.load_table import load_table
from data_contract.validation.models import (
    TableReport,
    ValidationReport,
)
from data_contract.validation.phases import PHASES, PhaseContext
from data_contract.validation.post import (
    build_rejected_rows,
    build_run_metadata,
    populate_by_check,
    populate_metrics,
)


DEFAULT_OUTPUT_SUBDIR = "validations"


# ---------------------------------------------------------------------------
# Setup-step exit-code helper
# ---------------------------------------------------------------------------
#
# Five "try/except (ConfigError, OSError, SettingsError) -> print + return 1"
# blocks at the top of `run_validate_data` collapse onto one `_step()` helper.
# Setup helpers raise `_AbortRun` after printing; the top-level `try/except` in
# `run_validate_data` catches and returns the carried exit code.


class _AbortRun(Exception):
    """Bail out of `run_validate_data` early with a fixed exit code. Raised by
    `_step()` once it has printed the user-facing error to stderr."""

    def __init__(self, exit_code: int = 1) -> None:
        self.exit_code = exit_code


def _step(label: str, fn):
    """Run `fn()`; on `ConfigError | OSError | SettingsError`, print the message
    with `label` as prefix and abort the run with exit code 1. `label` may
    optionally end with ": " for "verb the noun: <error>" framings (e.g.
    "failed to load type registry {p}: ")."""
    try:
        return fn()
    except (ConfigError, OSError, SettingsError) as e:
        print(f"validate-data: {label}{e}", file=sys.stderr)
        raise _AbortRun() from e


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_validate_data(
    *,
    epic: str,
    table_filter: str | None,
    input_dir: Path | None,
    output_dir: Path | None,
    epic_root: Path,
    types_path: Path,
    strict_columns: bool = False,
    json_to_stdout: bool = False,
) -> int:
    """CLI entry point. Returns exit code (0 / 1 / 2).

    Path resolution:
      - input_dir: None -> epic_dir; relative -> epic_dir/<relative>; absolute -> as-is.
        The actual file location is determined by `file_pattern` in
        validation.yaml.
      - output_dir: None -> epic_dir/"validations"; relative -> epic_dir/<relative>;
        absolute -> as-is.
    """
    try:
        epic = validate_epic_name(epic)
    except InvalidEpicName as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1

    epic_dir = epic_root / epic
    configs_dir = epic_dir / "configs"
    validation_yaml = configs_dir / "validation.yaml"
    parsers_yaml_path = types_path.parent / "parsers.yaml"
    input_dir = _resolve_epic_path(input_dir, epic_dir, default_subdir=None)
    out_dir = _resolve_epic_path(output_dir, epic_dir, default_subdir=DEFAULT_OUTPUT_SUBDIR)

    try:
        config = _step("", lambda: ValidationConfig.from_yaml(validation_yaml, parsers_yaml_path))
        settings = _step("", load_settings)
        type_registry = _step(
            f"failed to load type registry {types_path}: ",
            lambda: load_type_registry(types_path),
        )

        def _load_target():
            target_path = resolve_target_path(
                config.target, repo_root=types_path.parent.parent, epic_dir=epic_dir,
            )
            return load_target_config(target_path)
        target_config = _step("", _load_target)
        type_registry = type_registry.with_target(target_config)

        contracts_dir = config.contracts_folder if config.contracts_folder else (epic_dir / "contracts")
        contracts_by_table = _step(
            f"failed to load contracts under {contracts_dir}: ",
            lambda: _load_contracts(contracts_dir),
        )
    except _AbortRun as e:
        return e.exit_code

    if not input_dir.is_dir():
        print(f"validate-data: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    try:
        table_configs = _resolve_table_configs(config, contracts_by_table, table_filter)
    except ConfigError as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    if not table_configs:
        print(
            f"validate-data: no tables to validate (CLI --table={table_filter!r}, "
            f"contracts found={sorted(contracts_by_table)})",
            file=sys.stderr,
        )
        return 1

    validation_start = time.perf_counter()
    try:
        table_frames, table_eager_frames, table_reports = _phase_a_load_and_check(
            config=config,
            table_configs=table_configs,
            contracts_by_table=contracts_by_table,
            input_dir=input_dir,
            strict_columns=strict_columns,
            type_registry=type_registry,
            settings=settings,
        )
    except ConfigError as e:
        # Config errors raised by per-table setup (field_mapping incompatible
        # with positional mode, colliding positional rename, ...) are user
        # misconfiguration -- exit 1 with a friendly message rather than a
        # traceback. Runtime data errors are still routed through Violations
        # by each phase / check, so they don't land here.
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    run_cross_table_fk(
        table_reports=table_reports,
        table_frames=table_frames,
        table_configs=table_configs,
        contracts_by_table=contracts_by_table,
    )
    _finalize_reports(
        table_reports=table_reports,
        table_configs=table_configs,
        table_eager_frames=table_eager_frames,
        contracts_by_table=contracts_by_table,
        settings=settings,
        type_registry=type_registry,
        target_config=target_config,
    )
    validation_duration_ms = int((time.perf_counter() - validation_start) * 1000)

    generated_at = now_iso_z()
    run_metadata = build_run_metadata(
        epic=epic, generated_at=generated_at, duration_ms=validation_duration_ms,
        config=config, target_config=target_config,
        contracts_by_table=contracts_by_table, types_path=types_path,
        table_reports=table_reports,
    )

    final_report = ValidationReport(
        epic=epic, generated_at=generated_at, table_reports=table_reports,
        settings=settings, run_metadata=run_metadata,
    )

    from data_contract.validation.report.writer import write_all
    out_dir.mkdir(parents=True, exist_ok=True)
    write_all(final_report, out_dir, contracts_by_table)
    _print_console_summary(final_report, out_dir)

    if json_to_stdout:
        import json as _json
        from data_contract.validation.report.json_report import render_json
        print(_json.dumps(render_json(final_report, contracts_by_table), indent=2))

    return 2 if final_report.has_errors else 0


# ---------------------------------------------------------------------------
# Phase A: per-table load + per-table checks
# ---------------------------------------------------------------------------


def _phase_a_load_and_check(
    *,
    config: ValidationConfig,
    table_configs: dict[str, TableValidationConfig],
    contracts_by_table: dict[str, Contract],
    input_dir: Path,
    strict_columns: bool,
    type_registry: TypeRegistry,
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any], list[TableReport]]:
    table_frames: dict[str, Any] = {}
    table_eager_frames: dict[str, Any] = {}
    table_reports: list[TableReport] = []
    for table_name in table_configs:
        table_cfg = table_configs[table_name]
        contract = contracts_by_table[table_name]
        report = TableReport(
            table=table_name,
            contract_version=contract.version,
            pk_fields=[f.name for f in contract.primary_key_fields()],
            input_files=[],
        )
        frame = _validate_one_table(
            config=config, table_cfg=table_cfg, contract=contract,
            input_dir=input_dir, report=report,
            strict_columns=strict_columns, type_registry=type_registry,
            settings=settings,
        )
        if frame is not None:
            table_frames[table_name] = frame
            table_eager_frames[table_name] = frame.collect()
        table_reports.append(report)
    return table_frames, table_eager_frames, table_reports


def _validate_one_table(
    *,
    config: ValidationConfig,
    table_cfg: TableValidationConfig,
    contract: Contract,
    input_dir: Path,
    report: TableReport,
    strict_columns: bool,
    type_registry: TypeRegistry,
    settings: Settings,
) -> Any:
    """Load + per-table checks. Returns the unified LazyFrame on success, or
    None when the table couldn't even be loaded.
    """
    loaded = load_table(
        config=config, table_cfg=table_cfg, contract=contract,
        input_dir=input_dir, report=report,
        strict_columns=strict_columns, settings=settings,
    )
    if loaded is None:
        return None

    pk_cols = [f.name for f in contract.primary_key_fields()]
    ctx = PhaseContext(
        df=loaded.df, contract=contract, gates=table_cfg.checks,
        type_registry=type_registry, report=report,
        data_columns=loaded.data_columns, pk_cols=pk_cols,
        emit=emit_from_lazy,
    )
    for phase in PHASES:
        phase(ctx)
    df = ctx.df

    populate_by_check(report, table_cfg.checks)
    populate_metrics(report, df, contract, table_cfg.metrics, type_registry)

    return df.lazy()


# ---------------------------------------------------------------------------
# Post-check finalization: profile, score, rejected rows, by_check refresh
# ---------------------------------------------------------------------------


def _finalize_reports(
    *,
    table_reports: list[TableReport],
    table_configs: dict[str, TableValidationConfig],
    table_eager_frames: dict[str, Any],
    contracts_by_table: dict[str, Contract],
    settings: Settings,
    type_registry: TypeRegistry,
    target_config,
) -> None:
    from data_contract.validation.report.dimensions import compute_table_score
    from data_contract.validation.report.profile import build_table_profile

    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        tr.score = compute_table_score(total_rows=tr.total_rows, violations=tr.violations)
        tr.profile = build_table_profile(
            contract, type_registry,
            total_rows=tr.total_rows, type_format=target_config.name,
        )
        eager = table_eager_frames.get(tr.table)
        if eager is not None:
            build_rejected_rows(
                tr=tr, eager_df=eager, contract=contract,
                cap=settings.rejected_row_cap, type_registry=type_registry,
            )
        # Re-compute by_check now that cross-table FK violations are stitched
        # in. `_validate_one_table` populated it once for per-table checks; FK
        # results land in `report.violations` afterwards so the grid must be
        # refreshed here for tables that have FK fields.
        table_cfg = table_configs.get(tr.table)
        if table_cfg is not None:
            populate_by_check(tr, table_cfg.checks)


# ---------------------------------------------------------------------------
# Path / contract / table resolution helpers
# ---------------------------------------------------------------------------


def _resolve_epic_path(supplied: Path | None, epic_dir: Path, *, default_subdir: str | None) -> Path:
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


def _resolve_table_configs(
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


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------


def _print_console_summary(report: ValidationReport, out_dir: Path) -> None:
    counts = report.summary_counts
    status = "FAIL" if report.has_errors else "OK"
    print(f"[VALIDATE-DATA-{status}] epic {report.epic} -> {out_dir}")
    print(
        f"  totals: {counts['error']} errors, "
        f"{counts['warning']} warnings, {counts['info']} info"
    )
    for tr in report.table_reports:
        n_err = sum(1 for v in tr.violations if v.severity == "error")
        n_warn = sum(1 for v in tr.violations if v.severity == "warning")
        print(
            f"  {tr.table}: {tr.total_rows} rows, "
            f"{len(tr.input_files)} files, {n_err} errors, {n_warn} warnings"
        )
