"""Bronze fidelity runner: structural presence + TRY_CAST coercibility.

Bronze tables are all-STRING ingest. Two checks per logical table:
  (a) every contract field is present as a column in the bronze table
      (name match, case-sensitive; flags missing AND extra columns).
  (b) every non-null string value in each typed column is coercible to
      the contract's declared type via Trino's TRY_CAST.

Value constraints (`min_value`, `allowed_values`, etc.) are NOT checked
here -- that's silver's job after typing. STRING columns skip the
TRY_CAST step since bronze is already string.

Exit codes match the silver runner: 0 clean, 1 config error, 2
violations.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dq_core.errors import ConfigError
from dq_core.report_models import TableReport
from dq_core.type_mapping import Type
from dq_core.violations import Violation

from warehouse_validation import emit_sql
from warehouse_validation.setup import RunSetupError, prepare_run
from warehouse_validation.table_mapping import load_mapping
from warehouse_validation.type_coercion import trino_cast_type


SUBDIR = "bronze"


def run_validate_bronze(
    *,
    epic: str,
    table: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
    cli_args: list[str] | None = None,
) -> int:
    try:
        setup = prepare_run(
            epic=epic, table=table, connector_name=connector_name,
            epic_root=epic_root, output_dir=output_dir,
        )
    except RunSetupError as e:
        print(str(e), file=sys.stderr)
        return 1

    started = time.perf_counter()

    epic_dir = epic_root / setup.epic
    try:
        mapping = load_mapping(epic_dir, logical_name=table)
    except ConfigError as e:
        print(f"validate-bronze: {e}", file=sys.stderr)
        return 1

    pk_fields = [f.name for f in setup.contract.primary_key_fields()]
    table_report = TableReport(
        table=setup.config.table_name,
        contract_version=setup.contract.version,
        pk_fields=pk_fields,
        input_files=[],
    )

    try:
        bronze_cols = setup.connector.execute_columns(mapping.bronze)
    except Exception as e:
        print(
            f"validate-bronze: connector failure resolving columns of "
            f"{mapping.bronze}: {e}",
            file=sys.stderr,
        )
        return 1

    contract_field_names = {f.name for f in setup.contract.fields}
    missing = contract_field_names - bronze_cols
    extra = bronze_cols - contract_field_names

    for name in sorted(missing):
        table_report.violations.append(Violation(
            kind="bronze_missing_column",
            severity="error",
            table=setup.config.table_name,
            field=name,
            expected="column present in bronze table",
            offending_value=mapping.bronze,
        ))
    for name in sorted(extra):
        table_report.violations.append(Violation(
            kind="bronze_extra_column",
            severity="error",
            table=setup.config.table_name,
            field=name,
            expected="no columns beyond contract fields",
            offending_value=mapping.bronze,
        ))

    for f in setup.contract.fields:
        if f.name in missing or f.type is Type.STRING:
            continue
        try:
            cast = trino_cast_type(f.type)
        except ConfigError as e:
            print(f"validate-bronze: {e}", file=sys.stderr)
            return 1
        sql = (
            f'SELECT COUNT(*) FROM {mapping.bronze} '
            f'WHERE "{f.name}" IS NOT NULL '
            f'AND TRY_CAST("{f.name}" AS {cast}) IS NULL'
        )
        try:
            bad = setup.connector.execute_count(sql)
        except Exception as e:
            print(
                f"validate-bronze: connector failure on "
                f"{setup.config.table_name}.{f.name}: {e}",
                file=sys.stderr,
            )
            return 1
        if bad > 0:
            table_report.violations.append(Violation(
                kind="bronze_uncoercible",
                severity="error",
                table=setup.config.table_name,
                field=f.name,
                expected=f"coercible to {f.type.value}",
                offending_value=bad,
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    out_paths = emit_sql.write(
        setup=setup,
        table_report=table_report,
        duration_ms=duration_ms,
        cli_args=cli_args or [],
        subdir=SUBDIR,
    )

    has_errors = any(v.severity == "error" for v in table_report.violations)
    n_err = sum(1 for v in table_report.violations if v.severity == "error")
    label = "FAIL" if has_errors else "OK"
    print(
        f"[VALIDATE-BRONZE-{label}] epic {setup.epic} table "
        f"{setup.config.table_name} -> {setup.config.output_dir / SUBDIR}"
    )
    print(f"  totals: {n_err} errors")
    print(f"  reports: {', '.join(sorted(out_paths))}")
    return 2 if has_errors else 0
