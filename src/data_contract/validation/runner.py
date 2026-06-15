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
from data_contract.contract import Contract, FieldContract
from data_contract.core.epic import InvalidEpicName, validate_epic_name
from data_contract.core.yaml_io import load_yaml
from data_contract.data_parsers import get_by_name
from data_contract.errors import ConfigError
from data_contract.settings import Settings, SettingsError, load_settings
from data_contract.targets import load_target_config, resolve_target_path
from data_contract.type_mapping import TypeRegistry, load_type_registry
from data_contract.validation.config import (
    TableValidationConfig,
    ValidationConfig,
)
from data_contract.validation.models import (
    TableReport,
    ValidationReport,
)
from data_contract.validation.phases import PHASES, PhaseContext
from data_contract.validation.post import (
    build_rejected_rows,
    build_run_metadata,
    diagnose_missing_inputs,
    populate_by_check,
    populate_metrics,
)
from data_contract.table_checks.fk_existence import FkExistenceCheck as _FkCheckCls
from data_contract.violations import Violation


DEFAULT_OUTPUT_SUBDIR = "validations"


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
        config = ValidationConfig.from_yaml(validation_yaml, parsers_yaml_path)
    except (ConfigError, OSError) as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1

    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1

    try:
        type_registry = load_type_registry(types_path)
    except (ConfigError, OSError) as e:
        print(f"validate-data: failed to load type registry {types_path}: {e}", file=sys.stderr)
        return 1

    try:
        target_path = resolve_target_path(
            config.target, repo_root=types_path.parent.parent, epic_dir=epic_dir,
        )
        target_config = load_target_config(target_path)
    except (ConfigError, OSError) as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    type_registry = type_registry.with_target(target_config)

    contracts_dir = config.contracts_folder if config.contracts_folder else (epic_dir / "contracts")

    try:
        contracts_by_table = _load_contracts(contracts_dir)
    except (ConfigError, OSError) as e:
        print(f"validate-data: failed to load contracts under {contracts_dir}: {e}", file=sys.stderr)
        return 1

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
    _phase_b_cross_table_fk(
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
    resolved_pattern = table_cfg.file_pattern.replace("{table}", contract.table)
    paths = sorted(input_dir.glob(resolved_pattern))
    if not paths:
        diagnostic = diagnose_missing_inputs(
            input_dir=input_dir, file_pattern=table_cfg.file_pattern,
            resolved_pattern=resolved_pattern,
        )
        report.violations.append(Violation(
            kind="no_input_files", severity="error", table=contract.table,
            expected=diagnostic["expected"],
            offending_value=diagnostic["offending_value"],
        ))
        return None

    parser_cls = get_by_name(table_cfg.format)
    parser_params = config.effective_parser_params(table_cfg)
    parser = parser_cls(parser_params)
    # Side context consumed by `FileParser._apply_field_matching`. The
    # parser's `field_matching_policy` (per-parser / per-table) and the
    # global `similarity_threshold` (.env) together drive how the i-th data
    # column binds to the i-th contract field. Parsers ignore the attributes
    # when they don't need them. Not a generic "how to parse" knob -- just
    # context the matching layer reads.
    parser.contract_field_names = [f.name for f in contract.fields]
    parser.similarity_threshold = settings.similarity_threshold

    try:
        parsed = parser.read(paths, table_name_hint=contract.table)
    except Exception as e:
        report.violations.append(Violation(
            kind="parser_failure", severity="error", table=contract.table,
            expected=f"file readable by {table_cfg.format!r} parser",
            offending_value=str(e),
        ))
        return None
    frame = parsed.frame
    report.source_schemas = parsed.per_file_schemas
    report.parser_cls = parser_cls

    if table_cfg.field_mapping:
        # `field_mapping` does a header-to-contract rename at the runner
        # level. In positional mode the parser has already renamed
        # columns to contract names; mappings whose source isn't present
        # are silently skipped (the defensive guard) and `column_missing`
        # surfaces any underlying gap.
        present_cols = set(frame.collect_schema().names())
        applicable = {
            src: dst for src, dst in table_cfg.field_mapping.items()
            if src in present_cols
        }
        if applicable:
            frame = frame.rename(applicable)

    df = frame.collect()
    report.total_rows = df.height
    if "__source_file__" in df.columns:
        per_file = (
            df.group_by("__source_file__").len()
              .to_dict(as_series=False)
        )
        for fname, cnt in zip(per_file["__source_file__"], per_file["len"]):
            matched = next((p for p in paths if p.name == fname), Path(fname))
            report.input_files.append((matched, cnt))

    contract_field_names = contract.field_name_set()
    data_columns = {c for c in df.columns if c not in {"__source_file__", "__row_index__"}}
    checks = table_cfg.checks

    _run_source_schema_drift(
        contract=contract, report=report, parser=parser, gates=checks,
    )

    extra_severity = "error" if strict_columns else settings.extra_columns_severity
    if extra_severity != "ignore":
        for cname in sorted(data_columns - contract_field_names):
            report.violations.append(Violation(
                kind="extra_column", severity=extra_severity,
                table=contract.table, field=cname,
                expected=f"contract declares no field {cname!r}",
            ))

    pk_cols = [f.name for f in contract.primary_key_fields()]
    ctx = PhaseContext(
        df=df, contract=contract, gates=checks,
        type_registry=type_registry, report=report,
        data_columns=data_columns, pk_cols=pk_cols,
        emit=_emit_from_lazy,
    )
    for phase in PHASES:
        phase(ctx)
    df = ctx.df

    populate_by_check(report, checks)
    populate_metrics(report, df, contract, table_cfg.metrics, type_registry)

    return df.lazy()


def _run_source_schema_drift(
    *, contract: Contract, report: TableReport, parser, gates,
) -> None:
    """Optional source-schema drift checks. Both default off, both emit warnings.

    Each skips silently when no source file in this run reported the
    relevant schema component (e.g. CSV runs never trigger
    `field_types_from_sample` because CSV has no type info).
    """
    if not report.source_schemas:
        return
    if gates.is_enabled("field_names_from_sample"):
        from data_contract.validation.checks.sample_field_drift import (
            check_field_names_from_sample,
        )
        report.violations.extend(check_field_names_from_sample(
            table=contract.table,
            per_file_schemas=report.source_schemas,
            contract=contract,
        ))
    if gates.is_enabled("field_types_from_sample"):
        from data_contract.validation.checks.sample_field_drift import (
            check_field_types_from_sample,
        )
        report.violations.extend(check_field_types_from_sample(
            table=contract.table,
            per_file_schemas=report.source_schemas,
            contract=contract,
            parser=parser,
        ))


# ---------------------------------------------------------------------------
# Phase B: cross-table FK existence
# ---------------------------------------------------------------------------


def _phase_b_cross_table_fk(
    *,
    table_reports: list[TableReport],
    table_frames: dict[str, Any],
    table_configs: dict[str, TableValidationConfig],
    contracts_by_table: dict[str, Contract],
) -> None:
    """Cross-table FK checks. Gated per-CHILD-table, so a table opting out
    of `fk_existence` skips its own FK columns regardless of what the parent
    table opts into."""
    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        frame = table_frames.get(tr.table)
        if frame is None:
            continue
        child_cfg = table_configs.get(tr.table)
        if child_cfg is not None and not child_cfg.checks.is_enabled("fk_existence"):
            continue
        for fk_field in contract.foreign_key_fields():
            _run_fk_check(
                child_table=tr.table, child_frame=frame, fk_field=fk_field,
                contracts_by_table=contracts_by_table, table_frames=table_frames,
                report=tr,
            )


def _run_fk_check(
    *,
    child_table: str,
    child_frame: Any,
    fk_field: FieldContract,
    contracts_by_table: dict[str, Contract],
    table_frames: dict[str, Any],
    report: TableReport,
) -> None:
    if fk_field.foreign_key is None:
        return
    target_table = fk_field.foreign_key.get("table")
    target_column = fk_field.foreign_key.get("column")
    target_contract = contracts_by_table.get(target_table)
    target_frame = table_frames.get(target_table)
    if target_contract is None or target_frame is None:
        report.violations.append(Violation(
            kind="fk_target_table_not_loaded", severity="warning",
            table=child_table, field=fk_field.name,
            expected=f"target table {target_table!r} included in this run",
        ))
        return
    violating = _FkCheckCls.run_for_field(
        child_frame, fk_field.name, target_frame, target_column,
    )
    pk_cols = [f.name for f in contracts_by_table[child_table].primary_key_fields()]
    _emit_from_lazy(
        violating,
        kind=_FkCheckCls.VIOLATION_KIND, severity=_FkCheckCls.VIOLATION_SEVERITY,
        table=child_table, field=fk_field, pk_cols=pk_cols,
        expected=f"exists in {target_table}.{target_column}",
        report=report,
    )


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
# Violation emit + console summary
# ---------------------------------------------------------------------------


def _emit_from_lazy(
    lazy_or_none,
    *,
    kind: str,
    severity: str,
    table: str,
    field: FieldContract | None,
    pk_cols: list[str],
    expected: str,
    report: TableReport,
    pk_violation: bool = False,
) -> None:
    if lazy_or_none is None:
        return
    rows = lazy_or_none.collect().to_dicts()
    field_name = field.name if isinstance(field, FieldContract) else None
    for row in rows:
        pk_values = _extract_pk_values(row, pk_cols)
        offending = row.get(field_name) if field_name else None
        report.violations.append(Violation(
            kind=kind, severity=severity, table=table,
            field=field_name if field_name else None,
            source_file=row.get("__source_file__"),
            source_row=int(row["__row_index__"]) if row.get("__row_index__") is not None else None,
            pk_values=pk_values, offending_value=offending, expected=expected,
        ))


def _extract_pk_values(row: dict[str, Any], pk_cols: list[str]) -> dict[str, Any] | None:
    if not pk_cols:
        return None
    return {c: row.get(c) for c in pk_cols}


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
