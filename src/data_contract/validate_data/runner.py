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
from data_contract.targets import load_target_config, resolve_target_path
from data_contract.type_mapping import Type, TypeRegistry, load_type_registry
from data_contract.validate_data.checks.core_fields import (
    check_boolean_coercion,
    check_max_length,
    check_nullable,
    check_type_coercion,
    normalize_boolean_column,
    normalize_typed_column,
)
from data_contract.validate_data.checks.keys import (
    check_fk_existence,
    check_pk_uniqueness,
)
from data_contract.validate_data.config import (
    TableValidationConfig,
    ValidationConfig,
    ValidationSettings,
)  # TableValidationConfig is re-used by _resolve_table_configs.
from data_contract.validate_data.parsers import get_by_name
from data_contract.validate_data.violations import Violation


@dataclass
class RejectedRow:
    """Row-centric view of a violating source row, with full source-row context.

    Carries every contract field's value on the offending row so spec authors
    don't have to grep the source file to see the surrounding columns.
    Built once per affected (source_file, source_row) at report time.
    """
    source_file: str
    source_row: int
    pk_values: dict[str, Any]
    source_row_data: dict[str, Any]                # every contract field on this row
    violations: list[dict[str, Any]]               # each violation affecting this row
    worst_severity: str                            # "error" | "warning" | "info"


@dataclass
class TableReport:
    table: str
    contract_version: str
    pk_fields: list[str]
    input_files: list[tuple[Path, int]]
    violations: list[Violation] = dc_field(default_factory=list)
    total_rows: int = 0
    # Extensions for the gold-standard report (populated after all checks run).
    profile: Any = None                            # report.profile.TableProfile | None
    score: Any = None                              # report.dimensions.TableScore | None
    rejected_rows: list[RejectedRow] = dc_field(default_factory=list)
    rejected_rows_truncated: int = 0               # count beyond rejected_row_cap


@dataclass
class RunMetadata:
    """Run-level context surfaced in every report format.

    Closes the "two reports look identical but mean different things" gap:
    the target, gated checks, contract versions, and tool version that
    produced the run all flow into the `run` block.
    """
    epic: str
    generated_at: str
    duration_ms: int
    status: str                                    # "PASS" | "FAIL"
    status_reason: str
    tool_version: str
    target: dict[str, str] | None                  # {"name", "description"} | None
    checks_enabled: list[str]
    checks_disabled: list[str]
    checks_descriptions: dict[str, str]            # YAML-supplied, per check name
    contracts: dict[str, str]                      # table -> contract version
    types_yaml_path: str
    cli_args: list[str]


@dataclass
class ValidationReport:
    epic: str
    generated_at: str
    table_reports: list[TableReport]
    settings: ValidationSettings
    run_metadata: RunMetadata | None = None        # built by run_validate_data

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


DEFAULT_OUTPUT_SUBDIR = "validations"


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
        The actual file location is determined by the `file_pattern` in
        validation.yaml, which is a glob applied relative to input_dir. So
        `file_pattern: "sample/{table}*.xlsx"` searches `epic_dir/sample/`.
      - output_dir: None -> epic_dir/"validations"; relative -> epic_dir/<relative>;
        absolute -> as-is.
    """
    epic_dir = epic_root / epic
    configs_dir = epic_dir / "configs"
    validation_yaml = configs_dir / "validation.yaml"
    # Parser defaults (encoding, delimiter, header_row, null_tokens, ...) are
    # global config: a CSV is parsed the same way regardless of which epic
    # owns the data. They live next to types.yaml under the global config
    # folder rather than per-epic, so derive their location from --types.
    parser_yaml_dir = types_path.parent / "parsers"
    input_dir = _resolve_epic_path(input_dir, epic_dir, default_subdir=None)
    out_dir = _resolve_epic_path(output_dir, epic_dir, default_subdir=DEFAULT_OUTPUT_SUBDIR)

    try:
        config = ValidationConfig.from_yaml(validation_yaml, parser_yaml_dir)
    except (ConfigError, OSError) as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1

    try:
        type_registry = load_type_registry(types_path)
    except (ConfigError, OSError) as e:
        print(f"validate-data: failed to load type registry {types_path}: {e}", file=sys.stderr)
        return 1

    # Target overlay. validation.yaml requires `target:` to be set -- universal
    # logical types in the contract acquire concrete validation rules only when
    # mapped to a target database's physical types. Load the per-target YAML
    # and produce a merged TypeRegistry whose accessors return target-overridden
    # bounds / tokens / formats / length_unit / physical_type. The runner threads
    # this merged registry through to the checks unchanged.
    try:
        target_path = resolve_target_path(
            config.target,
            repo_root=types_path.parent.parent,
            epic_dir=epic_dir,
        )
        target_config = load_target_config(target_path)
    except (ConfigError, OSError) as e:
        print(f"validate-data: {e}", file=sys.stderr)
        return 1
    type_registry = type_registry.with_target(target_config)

    # contracts_folder precedence: YAML's contracts_folder -> <epic_dir>/contracts.
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
    selected_tables = list(table_configs)

    # Phase A: load every table's frame + per-table checks except cross-table FK.
    import time
    validation_start = time.perf_counter()
    table_frames: dict[str, Any] = {}
    table_eager_frames: dict[str, Any] = {}        # eager df, for profile + rejected_rows
    table_reports: list[TableReport] = []
    for table_name in selected_tables:
        table_cfg = table_configs[table_name]
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
            type_registry=type_registry,
        )
        if frame is not None:
            table_frames[table_name] = frame
            table_eager_frames[table_name] = frame.collect()
        table_reports.append(report)

    # Phase B: cross-table FK existence checks. Gated per-CHILD-table, so a
    # table opting out of `fk_existence` skips its own FK columns regardless
    # of what the parent table opts into.
    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        frame = table_frames.get(tr.table)
        if frame is None:
            continue
        child_table_cfg = table_configs.get(tr.table)
        if child_table_cfg is not None and not child_table_cfg.checks.is_enabled("fk_existence"):
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

    # Build per-table profile, score, and rejected-rows view BEFORE writing.
    # All three need the eager df (profile + rejected_rows) and the violation
    # list (score + rejected_rows). The profile and score are pure-Python and
    # cheap; rejected_rows respects config.settings.rejected_row_cap.
    from data_contract.validate_data.report.dimensions import compute_table_score
    from data_contract.validate_data.report.profile import build_table_profile

    for tr in table_reports:
        contract = contracts_by_table[tr.table]
        tr.score = compute_table_score(total_rows=tr.total_rows, violations=tr.violations)
        eager = table_eager_frames.get(tr.table)
        if eager is not None:
            tr.profile = build_table_profile(
                eager, contract, type_registry, type_format=target_config.name,
            )
            _build_rejected_rows(
                tr=tr,
                eager_df=eager,
                contract=contract,
                cap=config.settings.rejected_row_cap,
                type_registry=type_registry,
            )

    validation_duration_ms = int((time.perf_counter() - validation_start) * 1000)

    generated_at = now_iso_z()
    run_metadata = _build_run_metadata(
        epic=epic,
        generated_at=generated_at,
        duration_ms=validation_duration_ms,
        config=config,
        target_config=target_config,
        contracts_by_table=contracts_by_table,
        types_path=types_path,
        table_reports=table_reports,
    )

    final_report = ValidationReport(
        epic=epic,
        generated_at=generated_at,
        table_reports=table_reports,
        settings=config.settings,
        run_metadata=run_metadata,
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


_MAX_SAMPLE_FILES_LISTED = 10


def _diagnose_missing_inputs(
    *,
    input_dir: Path,
    file_pattern: str,
    resolved_pattern: str,
) -> dict[str, Any]:
    """Build a friendly explanation for a `no_input_files` violation.

    Reports the absolute base dir that was searched, the pattern as-declared,
    the pattern after `{table}` substitution, and a sample of files that DO
    live in the search dir (to help spot typos in the file_pattern).
    """
    abs_base = input_dir.resolve()
    if input_dir.is_dir():
        existing = sorted(
            (p.name + ("/" if p.is_dir() else ""))
            for p in input_dir.iterdir()
        )
        # If the pattern points at a subdirectory, also surface what's inside it.
        sub_listing: list[str] | None = None
        slash_idx = resolved_pattern.find("/")
        if slash_idx > 0:
            subdir = input_dir / resolved_pattern[:slash_idx]
            if subdir.is_dir():
                sub_listing = sorted(
                    (p.name + ("/" if p.is_dir() else ""))
                    for p in subdir.iterdir()
                )

        expected = (
            f"at least one file matching glob {file_pattern!r} "
            f"(resolved to {resolved_pattern!r}) under base \"{abs_base}\""
        )
        offending: dict[str, Any] = {
            "searched_base": str(abs_base),
            "file_pattern": file_pattern,
            "resolved_pattern": resolved_pattern,
            "existing_top_level": existing[:_MAX_SAMPLE_FILES_LISTED],
            "existing_top_level_count": len(existing),
        }
        if sub_listing is not None:
            offending["subdir_searched"] = resolved_pattern[:slash_idx]
            offending["existing_in_subdir"] = sub_listing[:_MAX_SAMPLE_FILES_LISTED]
            offending["existing_in_subdir_count"] = len(sub_listing)
    else:
        expected = (
            f"at least one file matching glob {file_pattern!r} "
            f"(resolved to {resolved_pattern!r}) under base \"{abs_base}\", "
            f"but the base directory does not exist"
        )
        offending = {
            "searched_base": str(abs_base),
            "file_pattern": file_pattern,
            "resolved_pattern": resolved_pattern,
            "base_exists": False,
        }
    return {"expected": expected, "offending_value": offending}


def _resolve_epic_path(supplied: Path | None, epic_dir: Path, *, default_subdir: str | None) -> Path:
    """Resolve `--input-dir` / `--output-dir`:

    - None + default_subdir set    -> epic_dir / default_subdir
    - None + no default_subdir     -> epic_dir
    - relative path                -> epic_dir / supplied
    - absolute path                -> supplied (use as-is)
    """
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
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        contract = Contract.from_dict(payload)
        out[contract.table] = contract
    return out


def _resolve_table_configs(
    config: ValidationConfig,
    contracts_by_table: dict[str, Contract],
    table_filter: str | None,
) -> dict[str, TableValidationConfig]:
    """Determine which tables to validate and produce a TableValidationConfig for each.

    Rules:
    - If validation.yaml's `tables:` block is omitted, every contract found
      under contracts_folder is validated using the defaults block.
    - If `tables:` is set, only those entries are validated; each must have
      a matching contract YAML or this raises ConfigError.
    - `--table <name>` on the CLI restricts further to that single table.
    """
    if config.is_filtered():
        # Tables listed but missing contracts -> hard error so silent mismatches don't pass.
        missing = [t for t in config.tables if t not in contracts_by_table]
        if missing:
            raise ConfigError(
                f"tables block lists {missing} but no matching contract YAMLs found"
            )
        resolved = dict(config.tables)
    else:
        # No filter -> auto-build a TableValidationConfig from defaults for every discovered contract.
        resolved = {t: config.build_table_entry(t) for t in contracts_by_table}

    if table_filter is not None:
        if table_filter not in resolved:
            return {}
        return {table_filter: resolved[table_filter]}
    return resolved


def _validate_one_table(
    *,
    config: ValidationConfig,
    table_cfg: TableValidationConfig,
    contract: Contract,
    input_dir: Path,
    report: TableReport,
    strict_columns: bool,
    type_registry: TypeRegistry,
) -> Any:
    """Load + per-table checks. Returns the unified LazyFrame on success, or
    None when the table couldn't even be loaded (no files / bad parser params).
    """
    # Substitute the `{table}` placeholder so a single `defaults.file_pattern`
    # like "{table}*.xlsx" works for the per-table-file layout.
    resolved_pattern = table_cfg.file_pattern.replace("{table}", contract.table)
    paths = sorted(input_dir.glob(resolved_pattern))
    if not paths:
        diagnostic = _diagnose_missing_inputs(
            input_dir=input_dir,
            file_pattern=table_cfg.file_pattern,
            resolved_pattern=resolved_pattern,
        )
        report.violations.append(Violation(
            kind="no_input_files",
            severity="error",
            table=contract.table,
            expected=diagnostic["expected"],
            offending_value=diagnostic["offending_value"],
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
    checks = table_cfg.checks

    # Structural: missing required fields.
    if checks.is_enabled("column_missing"):
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

    # Phase A1: boolean tokens first -- emit any unmatched-token violations,
    # then normalize the column in place so every downstream check sees
    # canonical Booleans. The normalisation runs even when the coercion check
    # is disabled, because downstream checks expect canonical pl.Boolean.
    #
    # Token source is per-field: `fc.data_values` (stamped onto the contract
    # at generation time) is authoritative. Targets do not control which
    # tokens are accepted -- the contract does.
    from data_contract.validate_data.checks.core_fields import _field_boolean_tokens
    for fc in contract.fields:
        if fc.type is not Type.BOOLEAN or fc.name not in data_columns:
            continue
        field_tokens = _field_boolean_tokens(fc, type_registry)
        if field_tokens is None:
            continue
        if checks.is_enabled("boolean_coercion"):
            accepted_list = sorted({*field_tokens.get("true", set()),
                                    *field_tokens.get("false", set())})
            _emit_from_lazy(
                check_boolean_coercion(df.lazy(), fc, type_registry),
                kind="boolean_coercion_violation", severity="error",
                table=contract.table, field=fc, pk_cols=pk_cols,
                expected=f"one of: {', '.join(accepted_list)}",
                report=report,
            )
        df = normalize_boolean_column(df, fc, type_registry)

    # Phase A2: typed coerce + normalize for every non-VARCHAR, non-BOOLEAN
    # field. Hoisted ABOVE nullable/max_length and per-constraint checks so
    # downstream checks see correctly-typed columns (Int64, Float64, Date,
    # Datetime). VARCHAR fields are skipped so they stay String for
    # check_max_length downstream. Normalisation runs even when type_coercion
    # is disabled, otherwise Polars dtype invariants downstream collapse.
    for fc in contract.fields:
        if fc.name not in data_columns:
            continue
        if fc.type in (Type.STRING, Type.TEXT, Type.UNKNOWN, Type.BOOLEAN):
            continue
        if checks.is_enabled("type_coercion"):
            _emit_from_lazy(
                check_type_coercion(df.lazy(), fc, type_registry, eager_df=df),
                kind="type_coercion_violation", severity="error",
                table=contract.table, field=fc, pk_cols=pk_cols,
                expected=_type_coercion_expected(fc, type_registry),
                report=report,
            )
        df = normalize_typed_column(df, fc, type_registry)

    # Phase A3: per-field nullable + max_length. Runs after typed normalization
    # so check_nullable sees nulls produced by failed coercion; check_max_length
    # only ever runs on VARCHAR (still String, never normalized).
    for fc in contract.fields:
        if fc.name not in data_columns:
            continue
        if checks.is_enabled("nullable"):
            _emit_from_lazy(check_nullable(df.lazy(), fc),
                            kind="nullable_violation", severity="error",
                            table=contract.table, field=fc, pk_cols=pk_cols,
                            expected="required", report=report)
        if checks.is_enabled("max_length"):
            # Prefer the target's physical_type label when set; falls back to
            # "string(N chars)" or "string(N bytes)" with no target.
            try:
                pt = type_registry.physical_type_for(fc)
            except Exception:
                pt = None
            unit = type_registry.length_unit_for(Type.STRING)
            ml_expected = (
                f"len <= {fc.max_length} ({pt})" if pt
                else f"len <= {fc.max_length} ({unit})"
            )
            _emit_from_lazy(check_max_length(df.lazy(), fc, type_registry),
                            kind="max_length_violation", severity="error",
                            table=contract.table, field=fc, pk_cols=pk_cols,
                            expected=ml_expected,
                            report=report)

    # Per-constraint dispatch via the constraint class's own check_data.
    # Each constraint is gated by its `name` so authors can disable individual
    # constraint families (e.g. `min_value: false`) without losing the others.
    for fc, check in contract.iter_field_checks():
        if fc.name not in data_columns:
            continue
        if not checks.is_enabled(check.constraint_cls.name):
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
    if checks.is_enabled("pk_uniqueness"):
        pk_violations = check_pk_uniqueness(df.lazy(), contract)
        if pk_violations is not None:
            _emit_from_lazy(
                pk_violations,
                kind="pk_not_unique",
                severity="error",
                table=contract.table,
                field=None,
                pk_cols=pk_cols,
                expected="PK must be unique",
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
        expected=f"exists in {target_table}.{target_column}",
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


def _type_coercion_expected(field: FieldContract, type_registry: TypeRegistry) -> str:
    """Concise human-readable description of the type_coercion rule.

    Goal: fit in a spreadsheet cell and read like a sentence a non-engineer
    can act on. Date / timestamp formats are surfaced as `YYYY-MM-DD`-style
    patterns rather than `%Y-%m-%d`, and integer / float rules don't carry
    a regex.
    """
    # When a target is active, prefer the target's physical type name in the
    # message so the spec author sees "valid VARCHAR2(100 BYTE)" instead of
    # the bare canonical name.
    label = None
    if type_registry is not None:
        try:
            label = type_registry.physical_type_for(field)
        except Exception:
            label = None
    if label is None:
        label = field.type.value
    if field.type in (Type.INT32, Type.INT64):
        return f"valid {label} (integer)"
    if field.type in (Type.FLOAT32, Type.FLOAT64):
        return f"valid {label} (decimal)"
    if field.type in (Type.DATE, Type.TIMESTAMP, Type.TIMESTAMP_TZ):
        formats = type_registry.parse_formats_for(field.type) if type_registry else ()
        if formats:
            humanised = ", ".join(_humanise_strftime(f) for f in formats)
            return f"valid {label} (e.g. {humanised})"
        return f"valid {label}"
    return f"valid {label}"


# Mapping used to convert Python strftime directives into readable templates
# like `YYYY-MM-DD HH:MM:SS`. Conflicts between %m (month) and %M (minute) are
# resolved by always rendering them as the same `MM`, because in date context
# the position disambiguates.
_STRFTIME_HUMAN: dict[str, str] = {
    "%Y": "YYYY", "%y": "YY",
    "%m": "MM",   "%B": "Month",  "%b": "Mon",
    "%d": "DD",   "%A": "Day",    "%a": "Day",
    "%H": "HH",   "%I": "HH",     "%p": "AM/PM",
    "%M": "MM",   "%S": "SS",     "%f": "ffffff",
    "%z": "+ZZZZ", "%Z": "TZ",
    "%j": "DDD",  "%U": "WW",     "%W": "WW",
    "%%": "%",
}


def _humanise_strftime(fmt: str) -> str:
    """Convert a Python strftime string into a `YYYY-MM-DD`-style template."""
    out: list[str] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            directive = fmt[i:i+2]
            out.append(_STRFTIME_HUMAN.get(directive, directive))
            i += 2
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def _describe_constraint(check: FieldCheck) -> str:
    cls = check.constraint_cls
    if cls.name == "min_value":
        op = ">" if check.params.get("strict") else ">="
        return f"{op} {check.value}"
    if cls.name == "max_value":
        op = "<" if check.params.get("strict") else "<="
        return f"{op} {check.value}"
    if cls.name == "allowed_values":
        return f"one of: {', '.join(repr(x) for x in (check.value or []))}"
    if cls.name == "pattern":
        return f"matches /{check.value}/"
    if cls.name == "format":
        return f"format {check.value}"
    if cls.name == "unique":
        return "unique across rows"
    return cls.VIOLATION_KIND or cls.name


# ---------------------------------------------------------------------------
# RunMetadata + RejectedRow builders (called once at end of run_validate_data)
# ---------------------------------------------------------------------------


def _build_run_metadata(
    *,
    epic: str,
    generated_at: str,
    duration_ms: int,
    config,
    target_config,
    contracts_by_table: dict[str, Contract],
    types_path: Path,
    table_reports: list[TableReport],
) -> RunMetadata:
    """Assemble the per-run context that every report format surfaces."""
    from data_contract import __version__ as _tool_version

    from data_contract.validate_data.report.strings import load_strings as _load_strings

    enabled: list[str] = []
    disabled: list[str] = []
    descriptions: dict[str, str] = {}
    # Global gates (from validation.yaml `checks:` block). Descriptions are
    # sourced from configs/report_strings.yaml -- they're global UI text, not
    # epic-specific config, so they don't belong in validation.yaml.
    try:
        descriptions_map = _load_strings().get("checks_sheet", "descriptions")
    except KeyError:
        descriptions_map = {}
    for name, spec in sorted(config.checks.specs.items()):
        (enabled if spec.enabled else disabled).append(name)
        descriptions[name] = descriptions_map.get(name, "")

    status = "FAIL" if any(
        v.severity == "error" for tr in table_reports for v in tr.violations
    ) else "PASS"

    n_err = sum(1 for tr in table_reports for v in tr.violations if v.severity == "error")
    n_warn = sum(1 for tr in table_reports for v in tr.violations if v.severity == "warning")
    if status == "FAIL":
        reason = f"{n_err} error(s) across {sum(1 for tr in table_reports if any(v.severity == 'error' for v in tr.violations))} table(s)"
    elif n_warn:
        reason = f"all checks clean ({n_warn} warning(s))"
    else:
        reason = "all checks clean"

    target = None
    if target_config is not None:
        target = {"name": target_config.name, "description": target_config.description}

    return RunMetadata(
        epic=epic,
        generated_at=generated_at,
        duration_ms=duration_ms,
        status=status,
        status_reason=reason,
        tool_version=_tool_version,
        target=target,
        checks_enabled=enabled,
        checks_disabled=disabled,
        checks_descriptions=descriptions,
        contracts={tr.table: tr.contract_version for tr in table_reports},
        types_yaml_path=str(types_path),
        cli_args=list(sys.argv[1:]),
    )


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _build_rejected_rows(
    *,
    tr: TableReport,
    eager_df,
    contract: Contract,
    cap: int,
    type_registry,
) -> None:
    """Populate `tr.rejected_rows` from the violations + eager df.

    Row-centric: aggregates all violations affecting the same (source_file,
    source_row) into a single RejectedRow. Carries the full source-row data
    (every contract field's value) so the report shows the surrounding
    columns, not just the offending field.
    """
    import polars as pl
    from data_contract.validate_data.report.dimensions import dimension_for
    from data_contract.validate_data.report.hints import hint_for

    # Group violations by (source_file, source_row); keep only those with both.
    grouped: dict[tuple[str, int], list[Violation]] = {}
    for v in tr.violations:
        if v.source_file and v.source_row is not None:
            grouped.setdefault((v.source_file, v.source_row), []).append(v)
    if not grouped:
        return

    # Sort by worst severity first, then by file/row for stable output.
    def _worst_sev(vs):
        return min(_SEVERITY_RANK.get(v.severity, 9) for v in vs)
    sorted_keys = sorted(
        grouped,
        key=lambda k: (_worst_sev(grouped[k]), k[0], k[1]),
    )
    truncated = max(len(sorted_keys) - cap, 0)
    sorted_keys = sorted_keys[:cap]
    tr.rejected_rows_truncated = truncated

    # Fast lookup: (source_file, source_row) -> dict of column values.
    if "__source_file__" in eager_df.columns and "__row_index__" in eager_df.columns:
        rows = eager_df.to_dicts()
        lookup = {(r["__source_file__"], int(r["__row_index__"])): r for r in rows}
    else:
        lookup = {}

    contract_field_names = [f.name for f in contract.fields]
    pk_names = tr.pk_fields

    for key in sorted_keys:
        src_file, src_row = key
        viols = grouped[key]
        row_data_full = lookup.get(key, {})
        # Only keep the contract fields (not __source_file__ / __row_index__).
        source_row_data = {
            name: row_data_full.get(name) for name in contract_field_names
        }
        pk_values = {name: row_data_full.get(name) for name in pk_names}

        rendered: list[dict[str, Any]] = []
        for v in viols:
            try:
                dim = dimension_for(v.kind).value
            except KeyError:
                dim = "unknown"
            try:
                hint = hint_for(v.kind)
            except KeyError:
                hint = ""
            # Physical type from the active target (if any).
            phys: str | None = None
            if type_registry is not None and v.field:
                fc = next((f for f in contract.fields if f.name == v.field), None)
                if fc is not None:
                    try:
                        phys = type_registry.physical_type_for(fc)
                    except Exception:
                        phys = None
            rendered.append({
                "check_id": (
                    f"{tr.table}.{v.field}.{v.kind}" if v.field
                    else f"{tr.table}.{v.kind}"
                ),
                "kind": v.kind,
                "dimension": dim,
                "severity": v.severity,
                "field": v.field,
                "physical_type": phys,
                "offending_value": v.offending_value,
                "expected": v.expected,
                "hint": hint,
            })
        worst = min(viols, key=lambda v: _SEVERITY_RANK.get(v.severity, 9)).severity

        tr.rejected_rows.append(RejectedRow(
            source_file=src_file,
            source_row=int(src_row),
            pk_values=pk_values,
            source_row_data=source_row_data,
            violations=rendered,
            worst_severity=worst,
        ))


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
