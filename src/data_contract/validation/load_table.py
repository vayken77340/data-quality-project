"""Per-table file load + binding to contract field names.

Owns the "read source files, surface drift / extra columns" work that used
to be inlined in `runner._validate_one_table`. The output `LoadedTable`
carries the eager `pl.DataFrame`, the per-data-column set, and the parser
class -- enough state for the runner's phase loop to operate without
reaching back through the parser instance.

Header-to-contract binding lives entirely on the contract: each
`FieldContract` carries a `source_name` (the raw header) and `name` (the
database identifier). The parser's field-matching policy looks at both,
so this module no longer needs a per-table rename block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dq_core.contract import Contract
from data_contract.data_parsers import get_by_name
from dq_core.settings import Settings
from data_contract.validation.config import TableValidationConfig, ValidationConfig
from dq_core.report_models import TableReport
from dq_core.violations import Violation


_MAX_SAMPLE_FILES_LISTED = 10


def diagnose_missing_inputs(
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


@dataclass
class LoadedTable:
    """Result of loading one table: eager DataFrame + the parser-emitted
    metadata downstream phases need. `data_columns` excludes the tracking
    columns (`__source_file__`, `__row_index__`) so the field iteration in
    `phases.PHASES` doesn't have to filter them every time."""
    df: Any                       # eager pl.DataFrame
    data_columns: set[str]
    parser_cls: type
    parser: Any                   # FileParser instance, still needed by drift checks


def load_table(
    *,
    config: ValidationConfig,
    table_cfg: TableValidationConfig,
    contract: Contract,
    input_dir: Path,
    report: TableReport,
    strict_columns: bool,
    settings: Settings,
) -> LoadedTable | None:
    """Glob the input directory, instantiate the parser, read all matched files,
    surface `extra_column` and source-schema drift. Returns `None` on a load
    failure that's already been recorded as a `Violation` on `report`.
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

    try:
        parsed = parser.read(
            paths,
            table_name_hint=contract.table,
            contract_fields=[(f.name, f.extract_name) for f in contract.fields],
            similarity_threshold=settings.similarity_threshold,
        )
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

    return LoadedTable(
        df=df, data_columns=data_columns, parser_cls=parser_cls, parser=parser,
    )


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
