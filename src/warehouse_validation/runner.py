"""Silver-conformance runner: one table end-to-end via SQL pushdown.

For each (field, FieldCheck) on the contract:
  * Ask sql_predicates.to_sql_predicate for the SQL fragment selecting
    violating rows. If None (no pushdown registered), skip.
  * Issue `SELECT COUNT(*) FROM "<table>" WHERE <predicate>` via the
    connector.
  * If count > 0, append a Violation whose `offending_value` is the count
    (not a row -- we never pull rows on the warehouse side).

Aggregate into a TableReport + ValidationReport and hand off to emit_sql.

Exit codes (matches data_contract.cli convention):
  0 = clean
  1 = config error (epic name, missing contract, unknown connector)
  2 = violations found
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dq_core.report_build import describe_constraint
from dq_core.report_models import TableReport
from dq_core.violations import Violation
from warehouse_validation import emit_sql
from warehouse_validation.setup import RunSetupError, prepare_run
from warehouse_validation.sql_predicates import to_sql_predicate


def run_validate_warehouse(
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

    pk_fields = [f.name for f in setup.contract.primary_key_fields()]
    table_report = TableReport(
        table=setup.config.table_name,
        contract_version=setup.contract.version,
        pk_fields=pk_fields,
        input_files=[],
    )

    for f, check in setup.contract.iter_field_checks():
        predicate = to_sql_predicate(f, check)
        if predicate is None:
            continue
        sql = (
            f'SELECT COUNT(*) FROM "{setup.config.table_name}" '
            f'WHERE {predicate}'
        )
        try:
            count = setup.connector.execute_count(sql)
        except Exception as e:
            print(
                f"validate-warehouse: connector failure on "
                f"{setup.config.table_name}.{f.name}.{check.constraint_cls.name}: {e}",
                file=sys.stderr,
            )
            return 1

        if count > 0:
            table_report.violations.append(Violation(
                kind=check.constraint_cls.VIOLATION_KIND,
                severity="error",
                table=setup.config.table_name,
                field=f.name,
                offending_value=count,
                expected=describe_constraint(check),
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    out_paths = emit_sql.write(
        epic=setup.epic,
        output_dir=setup.config.output_dir,
        config=setup.config,
        table_reports=[table_report],
        contracts_by_table={setup.config.table_name: setup.contract},
        duration_ms=duration_ms,
        cli_args=cli_args or [],
    )

    has_errors = any(v.severity == "error" for v in table_report.violations)
    n_err = sum(1 for v in table_report.violations if v.severity == "error")
    n_warn = sum(1 for v in table_report.violations if v.severity == "warning")
    label = "FAIL" if has_errors else "OK"
    print(
        f"[VALIDATE-WAREHOUSE-{label}] epic {setup.epic} table {setup.config.table_name} "
        f"-> {setup.config.output_dir}"
    )
    print(f"  totals: {n_err} errors, {n_warn} warnings")
    print(f"  reports: {', '.join(sorted(out_paths))}")
    return 2 if has_errors else 0
