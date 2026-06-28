"""Cross-layer reconciliation runner: bronze vs silver row count.

Two `COUNT(*)` queries diffed in process. The operator supplies
`--expected-delta` to absorb the known number of rows the bronze->silver
transformation drops (filtering, dedup, etc.). Default 0 -- any other
delta is a violation.

Phase 3 ships exact-match row-count reconciliation only. Aggregate
diffs (SUM/MIN/MAX/COUNT DISTINCT), per-column reconciliation, and
tolerance windows (% delta, absolute delta with floor) are deferred.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dq_core.errors import ConfigError
from dq_core.report_models import TableReport
from dq_core.violations import Violation

from warehouse_validation import emit_sql
from warehouse_validation.setup import RunSetupError, prepare_run
from warehouse_validation.table_mapping import load_mapping


SUBDIR = "reconcile"


def run_validate_reconcile(
    *,
    epic: str,
    table: str,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
    expected_delta: int = 0,
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
        print(f"validate-reconcile: {e}", file=sys.stderr)
        return 1

    pk_fields = [f.name for f in setup.contract.primary_key_fields()]
    table_report = TableReport(
        table=setup.config.table_name,
        contract_version=setup.contract.version,
        pk_fields=pk_fields,
        input_files=[],
    )

    try:
        bronze_n = setup.connector.execute_count(
            f'SELECT COUNT(*) FROM {mapping.bronze}'
        )
        silver_n = setup.connector.execute_count(
            f'SELECT COUNT(*) FROM {mapping.silver}'
        )
    except Exception as e:
        print(f"validate-reconcile: connector failure: {e}", file=sys.stderr)
        return 1

    actual_delta = bronze_n - silver_n
    if actual_delta != expected_delta:
        table_report.violations.append(Violation(
            kind="reconcile_row_count_mismatch",
            severity="error",
            table=setup.config.table_name,
            field=None,
            expected=f"bronze - silver == {expected_delta}",
            offending_value=(
                f"bronze={bronze_n} silver={silver_n} "
                f"actual_delta={actual_delta}"
            ),
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
        subdir=SUBDIR,
    )

    has_errors = any(v.severity == "error" for v in table_report.violations)
    label = "FAIL" if has_errors else "OK"
    print(
        f"[VALIDATE-RECONCILE-{label}] epic {setup.epic} table "
        f"{setup.config.table_name} -> {setup.config.output_dir / SUBDIR}"
    )
    print(
        f"  bronze={bronze_n} silver={silver_n} "
        f"actual_delta={actual_delta} expected_delta={expected_delta}"
    )
    print(f"  reports: {', '.join(sorted(out_paths))}")
    return 2 if has_errors else 0
