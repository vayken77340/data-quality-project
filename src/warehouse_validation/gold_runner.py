"""Gold assertions runner: execute SQL files, score against expected_count.

Each rule under `epics/<E>/rules/gold/` carries its own logical
target table (sidecar `table:`). The runner discovers all rules,
applies optional `--table` / `--rule` filters, executes each rule's
SQL via `connector.execute_scalar`, and emits a Violation when the
returned count differs from the sidecar's `expected_count`.

Violations are grouped by `rule.table` into TableReports; the report
writer treats them the same way silver/bronze/reconcile reports
look. Error-severity violations trip exit 2; warning-severity ones
are reported but do NOT trip exit 2 (matching data_contract's
warning policy).

Exit codes:
  0 = clean (no error-severity violations; warnings OK)
  1 = config error (epic name, missing rule, malformed sidecar, ...)
  2 = at least one error-severity violation
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dq_core.contract import Contract
from dq_core.errors import ConfigError
from dq_core.gates import Gates, GateSpec
from dq_core.report_models import TableReport
from dq_core.violations import Violation

from warehouse_validation import emit_sql
from warehouse_validation.gold_rules import (
    GoldRule,
    RULES_DIR_RELPATH,
    discover_rules,
)
from warehouse_validation.setup import RunSetupError, prepare_gold_run


SUBDIR = "gold"


@dataclass(frozen=True)
class _GoldConfig:
    """Minimal _RunMetadataConfig duck type for the gold run. Carries
    only `.checks: Gates` which build_run_metadata reads. Gold's
    Gates are keyed by rule name (not by constraint name) so the
    report's enabled-checks list reflects the rules that actually ran.
    """
    checks: Gates


def run_validate_gold(
    *,
    epic: str,
    table: str | None,
    rule: str | None,
    connector_name: str,
    epic_root: Path,
    output_dir: Path | None,
    cli_args: list[str] | None = None,
) -> int:
    try:
        gsetup = prepare_gold_run(
            epic=epic, connector_name=connector_name,
            epic_root=epic_root, output_dir=output_dir,
        )
    except RunSetupError as e:
        print(str(e), file=sys.stderr)
        return 1

    started = time.perf_counter()

    rules_dir = gsetup.epic_dir / RULES_DIR_RELPATH
    try:
        rules = discover_rules(rules_dir)
    except ConfigError as e:
        print(f"validate-gold: {e}", file=sys.stderr)
        return 1

    if rule is not None:
        rules = [r for r in rules if r.name == rule]
        if not rules:
            print(
                f"validate-gold: rule {rule!r} not found under {rules_dir}",
                file=sys.stderr,
            )
            return 1
    if table is not None:
        rules = [r for r in rules if r.table == table]

    by_table: dict[str, list[GoldRule]] = {}
    for r in rules:
        by_table.setdefault(r.table, []).append(r)

    table_reports: list[TableReport] = []
    contracts_by_table: dict[str, Contract] = {}

    for tbl, tbl_rules in by_table.items():
        contract_path = gsetup.epic_dir / "contracts" / f"{tbl}.yaml"
        contract_version = "(none)"
        pk_fields: list[str] = []
        if contract_path.is_file():
            try:
                contract = Contract.load(contract_path)
            except (ConfigError, OSError) as e:
                print(f"validate-gold: {e}", file=sys.stderr)
                return 1
            contract_version = contract.version
            pk_fields = [f.name for f in contract.primary_key_fields()]
            contracts_by_table[tbl] = contract

        tr = TableReport(
            table=tbl,
            contract_version=contract_version,
            pk_fields=pk_fields,
            input_files=[],
        )

        for r in tbl_rules:
            try:
                raw = gsetup.connector.execute_scalar(r.sql)
            except Exception as e:
                print(
                    f"validate-gold: connector failure on rule "
                    f"{r.name!r}: {e}",
                    file=sys.stderr,
                )
                return 1
            if raw is None:
                print(
                    f"validate-gold: rule {r.name!r} returned NULL "
                    f"(expected a scalar count)",
                    file=sys.stderr,
                )
                return 1
            count = int(raw)
            if count != r.expected_count:
                tr.violations.append(Violation(
                    kind="gold_rule_violation",
                    severity=r.severity,
                    table=tbl,
                    field=None,
                    expected=f"{r.name}: count == {r.expected_count}",
                    offending_value=f"actual_count={count}",
                ))
        table_reports.append(tr)

    config = _GoldConfig(checks=Gates(specs={
        r.name: GateSpec(enabled=True) for r in rules
    }))

    duration_ms = int((time.perf_counter() - started) * 1000)
    out_paths = emit_sql.write(
        epic=gsetup.epic,
        output_dir=gsetup.output_dir,
        config=config,
        table_reports=table_reports,
        contracts_by_table=contracts_by_table,
        duration_ms=duration_ms,
        cli_args=cli_args or [],
        subdir=SUBDIR,
    )

    n_failed = sum(len(tr.violations) for tr in table_reports)
    n_passed = len(rules) - n_failed
    has_errors = any(
        v.severity == "error"
        for tr in table_reports for v in tr.violations
    )
    label = "FAIL" if has_errors else "OK"
    print(
        f"[VALIDATE-GOLD-{label}] epic {gsetup.epic} -> "
        f"{gsetup.output_dir / SUBDIR}"
    )
    print(
        f"  rules: {n_passed} passed, {n_failed} failed "
        f"across {len(by_table)} table(s)"
    )
    print(f"  reports: {', '.join(sorted(out_paths))}")
    return 2 if has_errors else 0
