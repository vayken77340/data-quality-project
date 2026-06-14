"""CLI dispatcher. Argument parsing + sub-command dispatch only.

Business logic lives in:
  * `generation/pipeline.py` -- spec -> contract per-epic processing.
  * `generation/backfill.py` -- history backfill loop.
  * `generation/validate_contract.py` -- on-disk contract validation.
  * `validation/runner.py` -- data validation orchestration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from data_contract import __version__
from data_contract._util import dump_yaml
from data_contract.core.epic import (
    InvalidEpicName,
    discover_epics,
    validate_epic_name,
)
from data_contract.contract import Contract
from data_contract.errors import ConfigError
from data_contract.generation.builder import (
    drift_path_for,
    history_path_for_table,
)
from data_contract.generation.catalog import (
    DEFAULT_CONSTRAINTS_DOC,
    regen_constraint_doc,
    would_regen_change,
)
from data_contract.generation.config import SPECS_PARSING_FILENAME
from data_contract.generation.drift import diff_contracts
from data_contract.generation.pipeline import Outcome, print_drift_summary, process_epic
from data_contract.generation.schema_export import (
    DEFAULT_SCHEMA_OUT,
    write_contract_json_schema,
)
from data_contract.settings import load_settings
from data_contract.type_mapping import load_type_registry


DEFAULT_TYPES_PATH = Path("configs/types.yaml")
DEFAULT_EPIC_ROOT = Path("epics")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return _cmd_generate_or_lint(args, write=True)
    if args.command == "lint":
        return _cmd_generate_or_lint(args, write=False)
    if args.command == "drift":
        return _cmd_drift(args)
    if args.command == "export-schema":
        return _cmd_export_schema(args)
    if args.command == "validate-contract":
        return _cmd_validate_contract(args)
    if args.command == "regen-docs":
        return _cmd_regen_docs(args)
    if args.command == "validate-data":
        return _cmd_validate_data(args)
    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _epic_arg_type(raw: str) -> str:
    """argparse `type=` adapter for `--epic`. Surfaces validation errors as
    the standard `argument --epic: <reason>` argparse message."""
    try:
        return validate_epic_name(raw)
    except InvalidEpicName as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data-contract",
        description="Spec-driven data contract generator.",
    )
    p.add_argument("--version-info", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    gen = sub.add_parser(
        "generate",
        help="Build contracts for one epic, or all epics if --epic is omitted.",
    )
    _add_generate_args(gen)
    gen.add_argument(
        "--no-backfill",
        action="store_true",
        help="Skip auto-generating history snapshots for sibling configs with versions older than the target.",
    )
    gen.add_argument(
        "--skip-self-check",
        action="store_true",
        help="Skip the post-build invariant pass (PK not nullable, FK targets exist, etc.). Use only in emergencies.",
    )

    lint = sub.add_parser(
        "lint",
        help="Validate the spec end-to-end without writing any artifacts. Same exit codes as generate.",
    )
    _add_generate_args(lint)

    drift = sub.add_parser(
        "drift",
        help="Compute drift between two existing history snapshots without regenerating.",
    )
    drift.add_argument("--epic", required=True, type=_epic_arg_type)
    drift.add_argument("--table", required=True)
    drift.add_argument("--from", dest="from_version", required=True, help="Older history version, e.g. 1.0.")
    drift.add_argument("--to", dest="to_version", required=True, help="Newer history version, e.g. 2.0.")
    drift.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    drift.add_argument("--write", action="store_true", help="Also write the drift YAML to contracts/drift/.")

    export = sub.add_parser(
        "export-schema",
        help="Render the contract JSON Schema (derived from the constraint registry).",
    )
    export.add_argument(
        "--out",
        default=str(DEFAULT_SCHEMA_OUT),
        help=f"Output path (default: {DEFAULT_SCHEMA_OUT}).",
    )

    validate_c = sub.add_parser(
        "validate-contract",
        help="Validate a contract YAML on disk: JSON Schema + semantic invariants.",
    )
    validate_c.add_argument(
        "--epic", default=None, type=_epic_arg_type,
        help="Validate every <epic>/contracts/*.yaml.",
    )
    validate_c.add_argument("--file", default=None, help="Validate a single YAML file. Mutually exclusive with --epic.")
    validate_c.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    validate_c.add_argument("--types", default=str(DEFAULT_TYPES_PATH))
    validate_c.add_argument(
        "--allow-unknown-constraints",
        action="store_true",
        help="Don't fail on contract constraint keys missing from the registry.",
    )
    validate_c.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON report to stdout instead of human-readable text.",
    )

    regen = sub.add_parser(
        "regen-docs",
        help="Rewrite the auto-generated catalog/format sections of docs/constraints.md.",
    )
    regen.add_argument("--path", default=None, help="Path to constraints.md (defaults to docs/constraints.md).")

    validate_d = sub.add_parser(
        "validate-data",
        help="Validate sample data against an epic's contracts.",
    )
    validate_d.add_argument("--epic", required=True, type=_epic_arg_type)
    validate_d.add_argument("--table", default=None, help="Restrict to one table.")
    validate_d.add_argument(
        "--input-dir",
        default=None,
        help=(
            "Base directory the validation.yaml's `file_pattern` is resolved against. "
            "Default: epics/<epic>/. Set this only as an override (e.g. to point at an "
            "external test dataset). Relative paths resolve under the epic dir; "
            "absolute paths are used as-is. The actual file location is determined "
            "by `file_pattern` (which may include subdirs like 'sample/{table}*.xlsx')."
        ),
    )
    validate_d.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Where to write the reports. Default: epics/<epic>/validations/. "
            "Relative paths resolve under the epic dir; absolute paths are used as-is."
        ),
    )
    validate_d.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    validate_d.add_argument("--types", default=str(DEFAULT_TYPES_PATH))
    validate_d.add_argument("--strict-columns", action="store_true", help="Extra columns -> error (default: warning).")
    validate_d.add_argument("--json", action="store_true", help="Also emit the structured JSON report to stdout.")

    return p


def _add_generate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--epic", default=None, type=_epic_arg_type,
        help=(
            "Epic name (e.g. 1118, 1118_MVP). Letters/digits/spaces/.-_; "
            "1-64 chars; must start and end with a letter or digit. "
            "If omitted, every epic under --epic-root is processed."
        ),
    )
    p.add_argument("--version", default=None, help="Pick a specific version config (mutually exclusive with --config).")
    p.add_argument("--config", default=None, help="Explicit path to a version config (requires --epic; mutually exclusive with --version).")
    p.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    p.add_argument("--types", default=str(DEFAULT_TYPES_PATH))
    p.add_argument("--allow-unknown-types", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------


def _cmd_generate_or_lint(args: argparse.Namespace, *, write: bool) -> int:
    if args.config is not None and args.epic is None:
        print("config error: --config requires --epic", file=sys.stderr)
        return 1

    epic_root = Path(args.epic_root)
    try:
        registry = load_type_registry(Path(args.types))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    if args.epic is not None:
        epics = [args.epic]
    else:
        epics = discover_epics(epic_root)
        if not epics:
            print(f"config error: no epics found under {epic_root}", file=sys.stderr)
            return 1
        print(f"discovered {len(epics)} epic(s): {', '.join(epics)}")

    backfill = write and not getattr(args, "no_backfill", False)
    skip_self_check = (not write) or getattr(args, "skip_self_check", False)

    settings = load_settings()
    # Spec-parsing defaults are global config: derived from the --types path
    # (same global config folder) rather than from per-epic directories.
    specs_parsing_path = Path(args.types).parent / SPECS_PARSING_FILENAME

    total = Outcome()
    for epic in epics:
        outcome = process_epic(
            epic=epic,
            epic_root=epic_root,
            registry=registry,
            specs_parsing_path=specs_parsing_path,
            version=args.version,
            explicit_config=Path(args.config) if args.config else None,
            allow_unknown_types=args.allow_unknown_types,
            backfill=backfill,
            write=write,
            skip_self_check=skip_self_check,
            settings=settings,
        )
        total.add(outcome)

    if len(epics) > 1:
        print(
            f"total: {total.built} built, {total.rejected} rejected"
            + (f", {len(total.epic_failures)} epic failure(s)" if total.epic_failures else "")
        )

    catalog_stale = False
    if not write:
        # Lint-only: fail if the catalog doc is out of date. Generate mode
        # doesn't gate because the developer is iterating; lint is the CI gate.
        if DEFAULT_CONSTRAINTS_DOC.is_file() and would_regen_change(DEFAULT_CONSTRAINTS_DOC):
            catalog_stale = True
            print(
                f"[LINT-FAIL] {DEFAULT_CONSTRAINTS_DOC} is out of date; "
                f"run `python -m data_contract regen-docs` to refresh",
                file=sys.stderr,
            )

    if total.epic_failures:
        return 1
    if total.rejected > 0 or catalog_stale:
        return 2
    return 0


def _cmd_regen_docs(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else DEFAULT_CONSTRAINTS_DOC
    try:
        changed = regen_constraint_doc(path)
    except FileNotFoundError:
        print(f"regen-docs: {path} not found", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"regen-docs: {e}", file=sys.stderr)
        return 1
    print(f"[REGEN] {path}{' (updated)' if changed else ' (unchanged)'}")
    return 0


def _cmd_export_schema(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        changed = write_contract_json_schema(out)
    except OSError as e:
        print(f"schema export error: {e}", file=sys.stderr)
        return 1
    if changed:
        print(f"[SCHEMA] wrote {out}")
    else:
        print(f"[SCHEMA] {out} unchanged")
    return 0


def _cmd_validate_data(args: argparse.Namespace) -> int:
    try:
        from data_contract.validation.runner import run_validate_data
    except ImportError as e:
        print(
            f"validate-data requires the [validate-data] extras. Install with:\n"
            f"  pip install data-contract[validate-data]\nDetails: {e}",
            file=sys.stderr,
        )
        return 1
    return run_validate_data(
        epic=args.epic,
        table_filter=args.table,
        input_dir=Path(args.input_dir) if args.input_dir else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        epic_root=Path(args.epic_root),
        types_path=Path(args.types),
        strict_columns=args.strict_columns,
        json_to_stdout=args.json,
    )


def _cmd_validate_contract(args: argparse.Namespace) -> int:
    from data_contract.generation.validate_contract import run_validate_contract
    return run_validate_contract(
        epic=args.epic,
        file=args.file,
        epic_root=Path(args.epic_root),
        types_path=Path(args.types),
        allow_unknown_constraints=args.allow_unknown_constraints,
        output_format="json" if args.json else "text",
    )


def _cmd_drift(args: argparse.Namespace) -> int:
    epic_root = Path(args.epic_root)
    contracts_dir = epic_root / args.epic / "contracts"
    from_file = history_path_for_table(contracts_dir, args.from_version, args.table)
    to_file = history_path_for_table(contracts_dir, args.to_version, args.table)
    for p in (from_file, to_file):
        if not p.is_file():
            print(f"history file not found: {p}", file=sys.stderr)
            return 1
    try:
        old_contract = Contract.load(from_file)
        new_contract = Contract.load(to_file)
    except Exception as e:
        print(f"failed to load history: {e}", file=sys.stderr)
        return 1
    report = diff_contracts(old_contract, new_contract)
    print_drift_summary(args.table, args.from_version, args.to_version, report)
    if args.write and not report.is_empty():
        out = drift_path_for(contracts_dir, args.table, args.from_version, args.to_version)
        dump_yaml(out, report.to_dict())
        try:
            rel = str(out.relative_to(contracts_dir)).replace("\\", "/")
        except ValueError:
            rel = str(out).replace("\\", "/")
        print(f"  wrote {rel}")
    return 0
