"""CLI dispatcher. argparse + dispatch only -- business logic lives in
each runner module:
  * validate-warehouse  -> warehouse_validation.runner.run_validate_warehouse (silver)
  * validate-bronze     -> warehouse_validation.bronze_runner.run_validate_bronze
  * validate-reconcile  -> warehouse_validation.reconcile_runner.run_validate_reconcile
  * validate-gold       -> warehouse_validation.gold_runner.run_validate_gold
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from dq_core.epic import InvalidEpicName, validate_epic_name
from warehouse_validation.bronze_runner import run_validate_bronze
from warehouse_validation.connectors import CONNECTOR_REGISTRY
from warehouse_validation.gold_runner import run_validate_gold
from warehouse_validation.reconcile_runner import run_validate_reconcile
from warehouse_validation.runner import run_validate_warehouse


DEFAULT_EPIC_ROOT = Path("epics")


def _epic_arg_type(raw: str) -> str:
    """argparse `type=` adapter for --epic; mirrors data_contract/cli.py."""
    try:
        return validate_epic_name(raw)
    except InvalidEpicName as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cli_args = list(argv) if argv is not None else None

    if args.command == "validate-warehouse":
        return run_validate_warehouse(
            epic=args.epic,
            table=args.table,
            connector_name=args.connector,
            epic_root=Path(args.epic_root),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            cli_args=cli_args,
        )
    if args.command == "validate-bronze":
        return run_validate_bronze(
            epic=args.epic,
            table=args.table,
            connector_name=args.connector,
            epic_root=Path(args.epic_root),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            cli_args=cli_args,
        )
    if args.command == "validate-reconcile":
        return run_validate_reconcile(
            epic=args.epic,
            table=args.table,
            connector_name=args.connector,
            epic_root=Path(args.epic_root),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            expected_delta=args.expected_delta,
            cli_args=cli_args,
        )
    if args.command == "validate-gold":
        return run_validate_gold(
            epic=args.epic,
            table=args.table,
            rule=args.rule,
            connector_name=args.connector,
            epic_root=Path(args.epic_root),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            cli_args=cli_args,
        )
    parser.print_help()
    return 1


def _add_common_args(
    p: argparse.ArgumentParser, *, table_required: bool = True,
) -> None:
    """Shared --epic/--table/--connector/--epic-root/--output-dir block.
    The four contract-bound subparsers (silver / bronze / reconcile)
    require --table; validate-gold passes table_required=False because
    its --table flag is an OPTIONAL filter (absent = every rule)."""
    p.add_argument("--epic", required=True, type=_epic_arg_type, help=(
        "Epic name (e.g. 1118). Letters/digits/spaces/.-_; 1-64 chars; "
        "must start and end with a letter or digit."
    ))
    p.add_argument("--table", required=table_required, default=None, help=(
        "Logical table name. Must match a contract YAML under "
        "epics/<epic>/contracts/<table>.yaml. For validate-gold the "
        "flag is OPTIONAL and filters rules by their sidecar table."
    ))
    p.add_argument(
        "--connector", required=True,
        choices=sorted(CONNECTOR_REGISTRY),
        help="Connector name. Currently supported: trino, oracle.",
    )
    p.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT), help=(
        "Root directory of the per-epic trees. Default: epics/."
    ))
    p.add_argument(
        "--output-dir", default=None,
        help=(
            "Where to write the reports. Default: "
            "epics/<epic>/validations_warehouse/. Relative paths resolve "
            "under the epic dir; absolute paths are used as-is. Bronze "
            "and reconcile runs use subdirs bronze/ and reconcile/."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="warehouse-validation",
        description="SQL-pushdown validator for warehouse tables.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    vw = sub.add_parser(
        "validate-warehouse",
        help=(
            "Validate the SILVER physical table against its contract via "
            "SQL pushdown. Issues SELECT COUNT(*) WHERE <predicate> per "
            "registered constraint; no row pulls."
        ),
    )
    _add_common_args(vw)

    vb = sub.add_parser(
        "validate-bronze",
        help=(
            "Check BRONZE fidelity: every contract field is a column in "
            "bronze (case-sensitive) AND every non-null string is "
            "TRY_CAST-coercible to the contract's declared type. No "
            "value constraints (those run on silver)."
        ),
    )
    _add_common_args(vb)

    vr = sub.add_parser(
        "validate-reconcile",
        help=(
            "Reconcile BRONZE vs SILVER row counts. COUNT(*) on each, "
            "diffed in process; --expected-delta absorbs known filtered "
            "drops. Any other delta is a violation."
        ),
    )
    _add_common_args(vr)
    vr.add_argument(
        "--expected-delta", type=int, default=0,
        help=(
            "Expected (bronze - silver) row count delta. Default 0. Set "
            "to the number of rows the bronze->silver transformation "
            "legitimately drops (filtering, dedup)."
        ),
    )

    vg = sub.add_parser(
        "validate-gold",
        help=(
            "Run GOLD assertion SQL files for an epic. Each rule's "
            "epics/<E>/rules/gold/<name>.sql is executed via "
            "connector.execute_scalar and its integer result compared "
            "to the sidecar's expected_count. No --table runs every "
            "rule; --table filters by sidecar table; --rule narrows to "
            "a single rule."
        ),
    )
    _add_common_args(vg, table_required=False)
    vg.add_argument(
        "--rule", default=None,
        help=(
            "Narrow execution to a single rule by name. Errors if the "
            "named rule does not exist under epics/<E>/rules/gold/."
        ),
    )

    return p
