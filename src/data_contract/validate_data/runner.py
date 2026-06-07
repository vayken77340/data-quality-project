"""Validation runner.

Orchestrates:
1. Load `validation.yaml` + per-parser YAMLs.
2. Load every contract YAML under the epic's `contracts/` directory.
3. For each table:
   - Glob the input directory for files matching `file_pattern`.
   - Instantiate the parser with the effective params, read into a unified LazyFrame.
   - Apply `field_mapping` to rename data columns to contract field names.
   - Run structural / core-field / per-constraint / PK uniqueness checks.
4. After all tables loaded, run cross-table FK existence checks.
5. Collect every violation into a ValidationReport.
6. Emit reports (Phase 5).

Polars is lazy-imported in this module's helpers — the orchestration entry
point is import-safe even without the validate-data extras (it'll error
gracefully on first use).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

from data_contract._util import now_iso_z
from data_contract.contract import Contract, FieldCheck, FieldContract
from data_contract.errors import ConfigError
from data_contract.validate_data.checks.core_fields import (
    check_max_length,
    check_nullable,
    check_type_coercion,
)
from data_contract.validate_data.checks.keys import (
    check_fk_existence,
    check_pk_uniqueness,
)
from data_contract.validate_data.config import (
    TableValidationConfig,
    ValidationConfig,
    ValidationSettings,
)
from data_contract.validate_data.parsers import get_by_name
from data_contract.validate_data.violations import Violation


@dataclass
class TableReport:
    table: str
    contract_version: str
    pk_fields: list[str]
    input_files: list[tuple[Path, int]]
    violations: list[Violation] = dc_field(default_factory=list)
    total_rows: int = 0


@dataclass
class ValidationReport:
    epic: str
    generated_at: str
    table_reports: list[TableReport]
    settings: ValidationSettings

    @property
    def has_errors(self) -> bool:
        return any(v.severity == "error" for tr in self.table_reports for v in tr.violations)

    @property
    def summary_counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for tr in self.table_reports:
            for v in tr.violations:
                if v.severity in out:
                    out[v.severity] += 1
        return out


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_validate_data(
    *,
    epic: str,
    table_filter: str | None,
    input_dir: Path,
    output_dir: Path | None,
    epic_root: Path,
    types_path: Path,
    strict_columns: bool = False,
    json_to_stdout: bool = False,
) -> int:
    """CLI entry point. Returns exit code (0 / 1 / 2)."""
    epic_dir = epic_root / epic
    contracts_dir = epic_dir / "contracts"
    configs_dir = epic_dir / "configs"
    validation_yaml = configs_dir / "validation.yaml"
    parser_yaml_dir = configs_dir / "parsers"
    out_dir = output_dir or (epic_dir / "validations")

    try:
        config = ValidationConfig.from_yaml(validation_yaml, parser_yaml_dir)
    except (ConfigError, OSError) as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1

    try:
        contracts_by_table = _load_contracts(contracts_dir)
    except (ConfigError, OSError) as e:
        print(f"validate-data: failed to load contracts under {contracts_dir}: {e}", file=sys.stderr)
        return 1

    if not input_dir.is_dir():
        print(f"validate-data: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    selected_tables = _select_tables(config, contracts_by_table, table_filter)
    if not selected_tables:
        print(
            f"validate-data: no tables to validate (filter={table_filter!r}, "
            f"configured={sorted(config.tables)}, contracts={sorted(contracts_by_table)})",
            file=sys.stderr,
        )
        return 1

    # Phase A: load every table's frame + per-table checks except cross-table FK.
    table_frames: dict[str, Any] = {}
    table_reports: list[TableReport] = []
    for table_name in selected_tables:
        table_cfg = config.tables[table_name]
        contract = contracts_by_table[table_name]
        report = TableReport(
            table=table_name,
            contract_version=contract.version,
            pk_fields=[f.name for f in contract.primary_key_fields()],
            input_files=[],
        )
        frame = _validate_one_table(
            config=config,
            table_cfg=table_cfg,
            contract=contract,
            input_dir=input_dir,
            report=report,
            strict_columns=strict_columns,
        )
        if frame is not None:
            table_frames[table_name] = frame
        table_reports.append(report)

    # Phase B: cross-table FK existence checks.
    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        frame = table_frames.get(tr.table)
        if frame is None:
            continue
        for fk_field in contract.foreign_key_fields():
            _run_fk_check(
                child_table=tr.table,
                child_frame=frame,
                fk_field=fk_field,
                contracts_by_table=contracts_by_table,
                table_frames=table_frames,
                report=tr,
            )

    final_report = ValidationReport(
        epic=epic,
        generated_at=now_iso_z(),
        table_reports=table_reports,
        settings=config.settings,
    )

    # Phase 5 writers — lazily imported to avoid pulling openpyxl/etc. at module load.
    from data_contract.validate_data.report.writer import write_all

    out_dir.mkdir(parents=True, exist_ok=True)
    write_all(final_report, out_dir, contracts_by_table)
    _print_console_summary(final_report, out_dir)

    if json_to_stdout:
        from data_contract.validate_data.report.json_report import render_json
        import json as _json
        print(_json.dumps(render_json(final_report, contracts_by_table), indent=2))

    return 2 if final_report.has_errors else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_contracts(contracts_dir: Path) -> dict[str, Contract]:
    if not contracts_dir.is_dir():
        raise ConfigError(f"contracts directory not found: {contracts_dir}")
    out: dict[str, Contract] = {}
    for yaml_path in sorted(contracts_dir.glob("*.yaml")):
        if yaml_path.name == "joins.yaml":
            continue
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        contract = Contract.from_dict(payload)
        out[contract.table] = contract
    return out


def _select_tables(
    config: ValidationConfig,
    contracts_by_table: dict[str, Contract],
    table_filter: str | None,
) -> list[str]:
    if table_filter is not None:
        if table_filter in config.tables and table_filter in contracts_by_table:
            return [table_filter]
        return []
    return [t for t in config.tables if t in contracts_by_table]


def _validate_one_table(
    *,
    config: ValidationConfig,
    table_cfg: TableValidationConfig,
    contract: Contract,
    input_dir: Path,
    report: TableReport,
    strict_columns: bool,
) -> Any:
    """Load + per-table checks. Returns the unified LazyFrame on success, or
    None when the table couldn't even be loaded (no files / bad parser params).
    """
    paths = sorted(input_dir.glob(table_cfg.file_pattern))
    if not paths:
        report.violations.append(Violation(
            kind="no_input_files",
            severity="error",
            table=contract.table,
            expected=f"at least one file matching glob {table_cfg.file_pattern!r}",
        ))
        return None

    parser_cls = get_by_name(table_cfg.format)
    parser_params = config.effective_parser_params(table_cfg)
    parser = parser_cls(parser_params)
    try:
        frame = parser.read(paths, table_name_hint=contract.table)
    except Exception as e:
        report.violations.append(Violation(
            kind="parser_failure",
            severity="error",
            table=contract.table,
            expected=f"file readable by {table_cfg.format!r} parser",
            offending_value=str(e),
        ))
        return None

    # Rename data columns to contract field names via field_mapping.
    if table_cfg.field_mapping:
        frame = frame.rename(dict(table_cfg.field_mapping))

    # Materialize once to collect total_rows and per-file counts. Polars's
    # `scan_csv` is lazy, but we need at least the row count + uniqueness work
    # against a stable frame.
    import polars as pl

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

    # Structural: missing required fields.
    for fname in sorted(contract_field_names - data_columns):
        report.violations.append(Violation(
            kind="column_missing",
            severity="error",
            table=contract.table,
            field=fname,
            expected=f"data file must contain column {fname!r}",
        ))

    # Structural: extra columns.
    extra_severity = config.settings.extra_columns_severity
    if strict_columns:
        extra_severity = "error"
    if extra_severity != "ignore":
        for cname in sorted(data_columns - contract_field_names):
            report.violations.append(Violation(
                kind="extra_column",
                severity=extra_severity,
                table=contract.table,
                field=cname,
                expected=f"contract declares no field {cname!r}",
            ))

    # Per-field core checks + per-constraint check_data.
    pk_cols = [f.name for f in contract.primary_key_fields()]
    for fc in contract.fields:
        if fc.name not in data_columns:
            continue
        _emit_from_lazy(check_nullable(df.lazy(), fc),
                        kind="nullable_violation", severity="error",
                        table=contract.table, field=fc, pk_cols=pk_cols,
                        expected="value is required", report=report)
        _emit_from_lazy(check_max_length(df.lazy(), fc),
                        kind="max_length_violation", severity="error",
                        table=contract.table, field=fc, pk_cols=pk_cols,
                        expected=f"length <= {fc.max_length}", report=report)
        _emit_from_lazy(check_type_coercion(df.lazy(), fc),
                        kind="type_coercion_violation", severity="error",
                        table=contract.table, field=fc, pk_cols=pk_cols,
                        expected=f"value must be {fc.type.value!r}", report=report)

    # Per-constraint dispatch via the constraint class's own check_data.
    for fc, check in contract.iter_field_checks():
        if fc.name not in data_columns:
            continue
        violating = check.constraint_cls.check_data(df.lazy(), fc, check)
        if violating is None:
            continue
        _emit_from_lazy(
            violating,
            kind=check.constraint_cls.VIOLATION_KIND,
            severity=check.constraint_cls.VIOLATION_SEVERITY,
            table=contract.table, field=fc, pk_cols=pk_cols,
            expected=_describe_constraint(check),
            report=report,
        )

    # PK uniqueness.
    pk_violations = check_pk_uniqueness(df.lazy(), contract)
    if pk_violations is not None:
        _emit_from_lazy(
            pk_violations,
            kind="pk_not_unique",
            severity="error",
            table=contract.table,
            field=None,
            pk_cols=pk_cols,
            expected="primary key must be unique",
            report=report,
            pk_violation=True,
        )

    return df.lazy()


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
            kind="fk_target_table_not_loaded",
            severity="warning",
            table=child_table,
            field=fk_field.name,
            expected=f"target table {target_table!r} included in this run",
        ))
        return
    violating = check_fk_existence(child_frame, fk_field.name, target_frame, target_column)
    pk_cols = [f.name for f in contracts_by_table[child_table].primary_key_fields()]
    _emit_from_lazy(
        violating,
        kind="fk_not_found",
        severity="error",
        table=child_table,
        field=fk_field,
        pk_cols=pk_cols,
        expected=f"value must exist in {target_table}.{target_column}",
        report=report,
    )


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
            kind=kind,
            severity=severity,
            table=table,
            field=field_name if field_name else None,
            source_file=row.get("__source_file__"),
            source_row=int(row["__row_index__"]) if row.get("__row_index__") is not None else None,
            pk_values=pk_values,
            offending_value=offending,
            expected=expected,
        ))


def _extract_pk_values(row: dict[str, Any], pk_cols: list[str]) -> dict[str, Any] | None:
    if not pk_cols:
        return None
    return {c: row.get(c) for c in pk_cols}


def _describe_constraint(check: FieldCheck) -> str:
    cls = check.constraint_cls
    if cls.name == "min_value":
        op = ">" if check.params.get("strict") else ">="
        return f"value {op} {check.value}"
    if cls.name == "max_value":
        op = "<" if check.params.get("strict") else "<="
        return f"value {op} {check.value}"
    if cls.name == "allowed_values":
        return f"value in {list(check.value or [])}"
    if cls.name == "pattern":
        return f"value matches /{check.value}/"
    if cls.name == "format":
        return f"value matches format {check.value!r}"
    if cls.name == "unique":
        return "value must be unique across rows"
    return cls.VIOLATION_KIND or cls.name


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
