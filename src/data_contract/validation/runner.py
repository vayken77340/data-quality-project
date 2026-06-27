"""Validation runner.

Orchestrates:
1. Setup (validation.yaml, parsers.yaml, .env, types.yaml, target, contracts)
   handled by `validation.setup.prepare_run`.
2. For each table:
   - Glob the input directory for files matching `file_pattern`.
   - Instantiate the parser, read into a unified LazyFrame
     (`validation.load_table.load_table`).
   - Run structural / core-field / per-constraint / table checks via
     `phases.PHASES`.
3. After all tables loaded, run cross-table FK existence checks
   (`validation.fk_pass.run_cross_table_fk`).
4. Build per-table profile, score, rejected_rows, by_check, metrics.
5. Build run metadata and dispatch to the four report writers.

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
from data_contract.errors import ConfigError
from data_contract.settings import Settings
from data_contract.type_mapping import TypeRegistry
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
from data_contract.validation.setup import (
    RunSetupError,
    prepare_run,
    resolve_table_configs,
)


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
    """CLI entry point. Returns exit code (0 / 1 / 2). Path resolution rules
    live in `setup.prepare_run`."""
    try:
        rs = prepare_run(
            epic=epic, input_dir=input_dir, output_dir=output_dir,
            epic_root=epic_root, types_path=types_path,
        )
    except RunSetupError as e:
        print(e, file=sys.stderr)
        return 1

    if not rs.input_dir.is_dir():
        print(f"validate-data: input directory not found: {rs.input_dir}", file=sys.stderr)
        return 1

    try:
        table_configs = resolve_table_configs(rs.config, rs.contracts_by_table, table_filter)
    except ConfigError as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    if not table_configs:
        print(
            f"validate-data: no tables to validate (CLI --table={table_filter!r}, "
            f"contracts found={sorted(rs.contracts_by_table)})",
            file=sys.stderr,
        )
        return 1

    validation_start = time.perf_counter()
    try:
        table_frames, table_eager_frames, table_reports = _phase_a_load_and_check(
            config=rs.config,
            table_configs=table_configs,
            contracts_by_table=rs.contracts_by_table,
            input_dir=rs.input_dir,
            strict_columns=strict_columns,
            type_registry=rs.type_registry,
            settings=rs.settings,
        )
    except ConfigError as e:
        # Per-table setup misconfig (field_mapping vs positional, colliding
        # positional rename, ...). Runtime data errors flow through Violations.
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    run_cross_table_fk(
        table_reports=table_reports,
        table_frames=table_frames,
        table_configs=table_configs,
        contracts_by_table=rs.contracts_by_table,
    )
    _finalize_reports(
        table_reports=table_reports,
        table_configs=table_configs,
        table_eager_frames=table_eager_frames,
        contracts_by_table=rs.contracts_by_table,
        settings=rs.settings,
        type_registry=rs.type_registry,
        target_config=rs.target_config,
    )
    validation_duration_ms = int((time.perf_counter() - validation_start) * 1000)

    generated_at = now_iso_z()
    run_metadata = build_run_metadata(
        epic=rs.epic, generated_at=generated_at, duration_ms=validation_duration_ms,
        config=rs.config, target_config=rs.target_config,
        contracts_by_table=rs.contracts_by_table, types_path=types_path,
        table_reports=table_reports,
    )

    final_report = ValidationReport(
        epic=rs.epic, generated_at=generated_at, table_reports=table_reports,
        settings=rs.settings, run_metadata=run_metadata,
    )

    from data_contract.validation.report.writer import write_all
    rs.out_dir.mkdir(parents=True, exist_ok=True)
    write_all(final_report, rs.out_dir, rs.contracts_by_table)
    _print_console_summary(final_report, rs.out_dir)

    if json_to_stdout:
        import json as _json
        from data_contract.validation.report.json_report import render_json
        print(_json.dumps(render_json(final_report, rs.contracts_by_table), indent=2))

    return 2 if final_report.has_errors else 0


# ---------------------------------------------------------------------------
# Per-table phase loop
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
        # Refresh by_check now that cross-table FK violations are stitched in
        # (per-table phases populated it before the FK pass ran).
        table_cfg = table_configs.get(tr.table)
        if table_cfg is not None:
            populate_by_check(tr, table_cfg.checks)


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
