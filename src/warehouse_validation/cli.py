"""CLI dispatcher. argparse + dispatch only -- business logic lives in
warehouse_validation.runner.run_validate_warehouse.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from dq_core.epic import InvalidEpicName, validate_epic_name
from warehouse_validation.connectors import CONNECTOR_REGISTRY
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
    if args.command == "validate-warehouse":
        return run_validate_warehouse(
            epic=args.epic,
            table=args.table,
            connector_name=args.connector,
            epic_root=Path(args.epic_root),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            cli_args=list(argv) if argv is not None else None,
        )
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="warehouse-validation",
        description="SQL-pushdown validator for warehouse tables.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    vw = sub.add_parser(
        "validate-warehouse",
        help=(
            "Validate one warehouse table against its contract via SQL "
            "pushdown. Issues SELECT COUNT(*) WHERE <predicate> per "
            "registered constraint; no row pulls."
        ),
    )
    vw.add_argument("--epic", required=True, type=_epic_arg_type, help=(
        "Epic name (e.g. 1118). Letters/digits/spaces/.-_; 1-64 chars; "
        "must start and end with a letter or digit."
    ))
    vw.add_argument("--table", required=True, help=(
        "Table name. Must match a contract YAML under "
        "epics/<epic>/contracts/<table>.yaml."
    ))
    vw.add_argument(
        "--connector", required=True,
        choices=sorted(CONNECTOR_REGISTRY),
        help="Connector name. Phase 2 ships: trino.",
    )
    vw.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT), help=(
        "Root directory of the per-epic trees. Default: epics/."
    ))
    vw.add_argument(
        "--output-dir", default=None,
        help=(
            "Where to write the reports. Default: "
            "epics/<epic>/validations_warehouse/. Relative paths resolve "
            "under the epic dir; absolute paths are used as-is."
        ),
    )
    return p
