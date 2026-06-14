from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from data_contract import __version__
from data_contract._util import (
    InvalidEpicName,
    dump_yaml,
    now_iso_z,
    validate_epic_name,
)


def _epic_arg_type(raw: str) -> str:
    """argparse `type=` adapter for `--epic`. Surfaces validation errors as
    the standard `argument --epic: <reason>` argparse message."""
    try:
        return validate_epic_name(raw)
    except InvalidEpicName as e:
        raise argparse.ArgumentTypeError(str(e)) from None
from data_contract.generation.config import (
    ALL_TABLES,
    SPECS_PARSING_FILENAME,
    Defaults,
    EpicConfig,
    MergedConfig,
    TableSelector,
    discover_version_configs,
    merge,
    select_version_config,
    version_sort_key,
)
from data_contract.contract import Contract, Rejection
from data_contract.generation.builder import (
    build_contract,
    drift_path_for,
    history_path_for_table,
    write_history_only,
    write_outputs,
)
from data_contract.generation.catalog import (
    DEFAULT_CONSTRAINTS_DOC,
    regen_constraint_doc,
    would_regen_change,
)
from data_contract.generation.docs import load_drift_entries, write_data_dictionary
from data_contract.generation.schema_export import DEFAULT_SCHEMA_OUT, write_contract_json_schema
from data_contract.generation.drift import DriftReport, diff_contracts
from data_contract.errors import ConfigError, SpecReaderError
from data_contract.generation.joins import (
    JoinsContract,
    JoinsRejection,
    build_joins_result,
    read_joins_sheet,
    write_joins_outputs,
)
from data_contract.generation.keys import (
    KeysData,
    build_pk_index,
    enrich_field_contract_list,
    read_keys_sheet,
)
from data_contract.settings import Settings, load_settings
from data_contract.generation.spec_reader import (
    iter_field_rows,
    list_table_spec_sheets,
    open_workbook,
    read_sheet,
)
from data_contract.type_mapping import TypeRegistry, load_type_registry


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


def _self_check_post_build(
    contracts_by_table: dict[str, Contract],
    joins_contract: "JoinsContract | None",
    type_registry: TypeRegistry,
    outcome: "_Outcome",
) -> None:
    """Post-build invariant pass over every freshly emitted contract + joins.

    Mirrors what `validate-contract` would catch on the on-disk YAMLs, but
    runs against the in-memory dataclasses so emission bugs are caught at the
    source. Errors are surfaced to stderr; the affected table is bumped onto
    `outcome.rejected` so the exit code reflects the failure. Canonical YAMLs
    are NOT deleted — left in place for the human to inspect.
    """
    from data_contract.generation.validate_contract import (
        check_invariants,
        check_joins_invariants,
    )

    peer_field_names = {t: c.field_name_set() for t, c in contracts_by_table.items()}

    for table, contract in contracts_by_table.items():
        peer_subset = {t: names for t, names in peer_field_names.items() if t != table}
        errors = check_invariants(
            contract, type_registry, peer_subset, allow_unknown_constraints=False,
        )
        if errors:
            outcome.rejected += 1
            print(f"[SELF-CHECK-FAIL] {table} - {len(errors)} invariant errors", file=sys.stderr)
            for err in errors:
                print(f"  - {err.render()}", file=sys.stderr)

    if joins_contract is not None:
        joins_dict = joins_contract.to_dict()
        errors = check_joins_invariants(joins_dict, peer_field_names)
        if errors:
            outcome.rejected += 1
            print(f"[SELF-CHECK-FAIL] joins - {len(errors)} invariant errors", file=sys.stderr)
            for err in errors:
                print(f"  - {err.render()}", file=sys.stderr)


@dataclass
class _Outcome:
    built: int = 0
    rejected: int = 0
    epic_failures: list[str] = field(default_factory=list)

    def add(self, other: "_Outcome") -> None:
        self.built += other.built
        self.rejected += other.rejected
        self.epic_failures.extend(other.epic_failures)


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
        epics = _discover_epics(epic_root)
        if not epics:
            print(f"config error: no epics found under {epic_root}", file=sys.stderr)
            return 1
        print(f"discovered {len(epics)} epic(s): {', '.join(epics)}")

    # `lint` never backfills (nothing to write). `generate` honors --no-backfill.
    backfill = write and not getattr(args, "no_backfill", False)

    # Self-check is on by default for `generate`; lint mode runs schema/invariant
    # checks already, so duplicating there isn't useful.
    skip_self_check = (not write) or getattr(args, "skip_self_check", False)

    settings = load_settings()
    # Spec-parsing defaults are global config: derived from the --types path
    # (same global config folder) rather than from per-epic directories.
    specs_parsing_path = Path(args.types).parent / SPECS_PARSING_FILENAME

    total = _Outcome()
    for epic in epics:
        outcome = _process_epic(
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


def _discover_epics(epic_root: Path) -> list[str]:
    if not epic_root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(epic_root.iterdir()):
        if child.is_dir() and (child / "configs").is_dir():
            out.append(child.name)
    return out


def _process_epic(
    *,
    epic: str,
    epic_root: Path,
    registry: TypeRegistry,
    specs_parsing_path: Path,
    version: str | None,
    explicit_config: Path | None,
    allow_unknown_types: bool,
    backfill: bool,
    write: bool,
    skip_self_check: bool = True,
    settings: Settings,
) -> _Outcome:
    outcome = _Outcome()
    epic_dir = epic_root / epic
    epic_configs_dir = epic_dir / "configs"

    print(f"--- epic {epic} ---")

    try:
        epic_config, reason = select_version_config(
            epic_configs_dir,
            version=version,
            explicit_path=explicit_config,
        )
        defaults = Defaults.from_yaml(specs_parsing_path)
        merged = merge(defaults, epic_config)
    except ConfigError as e:
        print(f"config error in epic {epic}: {e}", file=sys.stderr)
        outcome.epic_failures.append(epic)
        return outcome

    print(f"using {merged.epic_config_path} ({reason})")

    if backfill and settings.generate_history:
        _backfill_missing_history(
            epic_dir=epic_dir,
            epic_configs_dir=epic_configs_dir,
            defaults=defaults,
            target_version=epic_config.version,
            target_config_path=epic_config.path,
            registry=registry,
            allow_unknown_types=allow_unknown_types,
            settings=settings,
        )

    spec_path = epic_dir / "specs" / merged.spec_file_name
    contracts_dir = epic_dir / "contracts"

    try:
        wb = open_workbook(spec_path)
    except SpecReaderError as e:
        print(f"spec error in epic {epic}: {e}", file=sys.stderr)
        outcome.epic_failures.append(epic)
        return outcome

    spec_file_rel = str(spec_path).replace("\\", "/")
    selected = _resolve_table_selectors(merged, wb)

    # Read the keys sheet once. Errors here are replayed per-table.
    keys_data = read_keys_sheet(wb, merged.keys)
    pk_index = build_pk_index(keys_data.rows)

    # Track the table names that have already been emitted in this epic run.
    # If two sheets resolve to the same `contract.table`, the second one is
    # converted to a `duplicate_table_across_sheets` rejection and routed to a
    # disambiguated filename so the first sheet's output isn't clobbered.
    seen_tables: dict[str, str] = {}
    # Successfully built contracts, keyed by `contract.table`. Used by the
    # joins step to validate that referenced tables/columns exist.
    contracts_by_table: dict[str, Contract] = {}

    try:
        for selector in selected:
            sheet_name = selector.table_name
            read = read_sheet(wb, sheet_name, merged.column_mapping)
            if read.error is not None:
                rejection = Rejection(
                    version=merged.version,
                    epic=merged.epic,
                    generated_at=now_iso_z(),
                    spec_file=spec_file_rel,
                    spec_sheet=sheet_name,
                    table=sheet_name,
                    errors=keys_data.errors + [read.error],
                )
                _emit_result(rejection, contracts_dir, outcome, write=write)
                continue

            sheet_spec = read.spec
            assert sheet_spec is not None
            rows = list(iter_field_rows(wb, sheet_spec))
            result = build_contract(
                merged,
                sheet_spec,
                rows,
                type_registry=registry,
                spec_file_rel=spec_file_rel,
                table_name_from_config=selector.table_name,
                allow_unknown_types=allow_unknown_types,
            )
            result = _enrich_with_keys(
                result, keys_data, pk_index,
                fk_allow_violations=settings.allow_foreign_key_violation,
            )
            result = _check_duplicate_table(result, sheet_name, seen_tables)
            if isinstance(result, Contract):
                contracts_by_table[result.table] = result
            _emit_result(result, contracts_dir, outcome, write=write, settings=settings)

        joins_contract: JoinsContract | None = None
        if merged.joins is not None and settings.generate_join_contract:
            joins_contract = _emit_joins(
                merged, wb, contracts_by_table, contracts_dir, spec_file_rel, outcome,
                write=write, settings=settings,
            )
    finally:
        wb.close()

    if write and not skip_self_check and contracts_by_table:
        _self_check_post_build(contracts_by_table, joins_contract, registry, outcome)

    if write:
        if contracts_by_table:
            drift_aggregate = load_drift_entries(contracts_dir)
            docs_path = write_data_dictionary(
                epic_dir=epic_dir,
                epic=merged.epic,
                version=merged.version,
                spec_file=spec_file_rel,
                contracts=list(contracts_by_table.values()),
                joins=joins_contract,
                drift=drift_aggregate,
            )
            print(f"[DOCS] {_rel(docs_path, epic_dir)}")

    print(f"epic {epic}: {outcome.built} built, {outcome.rejected} rejected")
    return outcome


def _check_duplicate_table(
    result: Contract | Rejection,
    sheet_name: str,
    seen_tables: dict[str, str],
) -> Contract | Rejection:
    """If `result.table` was already produced by an earlier sheet in this run,
    convert it into a duplicate-table rejection and route it to a unique
    filename so the earlier sheet's canonical/history output isn't overwritten.
    """
    from data_contract.errors import RejectionError

    prior_sheet = seen_tables.get(result.table)
    if prior_sheet is None:
        seen_tables[result.table] = sheet_name
        return result

    # Rewrite the table identifier so write_outputs targets a distinct path.
    distinguished = f"{result.table}__from_sheet_{sheet_name}"
    return Rejection(
        version=result.version,
        epic=result.epic,
        generated_at=result.generated_at,
        spec_file=result.spec_file,
        spec_sheet=sheet_name,
        table=distinguished,
        errors=[RejectionError(
            kind="duplicate_table_across_sheets",
            field="table",
            value=result.table,
            message=(
                f"sheet {sheet_name!r} resolves to table {result.table!r}, "
                f"which was already produced by sheet {prior_sheet!r} earlier in this run; "
                f"check the spec's Table column for cross-sheet inconsistency"
            ),
        )],
    )


def _enrich_with_keys(
    result: Contract | Rejection,
    keys_data: KeysData,
    pk_index: dict[str, set[str]],
    *,
    fk_allow_violations: bool = False,
) -> Contract | Rejection:
    """Apply keys-sheet PK/FK enrichment to a fresh build result.

    - If `result` is already a Rejection: prepend any keys-sheet structural
      errors so reviewers see the upstream cause.
    - If `result` is a Contract and the keys-sheet had structural errors:
      convert to a Rejection carrying those errors.
    - If `result` is a Contract and the table has no row in the keys sheet:
      convert to a Rejection (keys_missing_table).
    - Otherwise: enrich in place; convert to Rejection only if enrichment
      collects any errors.
    """
    if isinstance(result, Rejection):
        if keys_data.errors:
            result.errors = list(keys_data.errors) + result.errors
        return result

    contract = result
    if keys_data.errors:
        return Rejection(
            version=contract.version,
            epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file,
            spec_sheet=contract.spec_sheet,
            table=contract.table,
            errors=list(keys_data.errors),
        )

    rows_for_table = keys_data.rows_for_table(contract.table)
    if not rows_for_table:
        from data_contract.errors import RejectionError
        return Rejection(
            version=contract.version,
            epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file,
            spec_sheet=contract.spec_sheet,
            table=contract.table,
            errors=[RejectionError(
                kind="keys_missing_table",
                field="table_name",
                value=contract.table,
                message=(
                    f"keys sheet has no row for table {contract.table!r}; "
                    f"every generated table must have an entry in the keys sheet"
                ),
            )],
        )

    _, errors, fk_warnings = enrich_field_contract_list(
        contract.fields,
        contract.table,
        rows_for_table,
        pk_index,
        fk_allow_violations=fk_allow_violations,
    )
    for w in fk_warnings:
        print(
            f"[WARN] {contract.table}: skipped FK enrichment ({w.kind}) on field {w.field!r} "
            f"because allow_foreign_key_violation=true (.env)",
            file=sys.stderr,
        )
    if errors:
        return Rejection(
            version=contract.version,
            epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file,
            spec_sheet=contract.spec_sheet,
            table=contract.table,
            errors=errors,
        )
    return contract


def _backfill_missing_history(
    *,
    epic_dir: Path,
    epic_configs_dir: Path,
    defaults: Defaults,
    target_version: str,
    target_config_path: Path,
    registry: TypeRegistry,
    allow_unknown_types: bool,
    settings: Settings,
) -> None:
    contracts_dir = epic_dir / "contracts"
    target_key = version_sort_key(target_version)

    sibling_paths = discover_version_configs(epic_configs_dir)
    siblings: list[EpicConfig] = []
    for p in sibling_paths:
        if p == target_config_path:
            continue
        try:
            siblings.append(EpicConfig.from_yaml(p))
        except ConfigError as e:
            print(f"  (skip backfill: bad sibling config {p}: {e})", file=sys.stderr)

    earlier = [s for s in siblings if version_sort_key(s.version) < target_key]
    earlier.sort(key=lambda s: version_sort_key(s.version))

    for sibling in earlier:
        try:
            merged = merge(defaults, sibling)
        except ConfigError as e:
            print(f"  (skip backfill v{sibling.version}: {e})", file=sys.stderr)
            continue

        spec_path = epic_dir / "specs" / merged.spec_file_name
        try:
            wb = open_workbook(spec_path)
        except SpecReaderError as e:
            print(f"  (skip backfill v{sibling.version}: {e})", file=sys.stderr)
            continue

        try:
            tables = _resolve_table_selectors(merged, wb)
            keys_data = read_keys_sheet(wb, merged.keys)
            pk_index = build_pk_index(keys_data.rows)
            seen_tables: dict[str, str] = {}
            for selector in tables:
                history_file = history_path_for_table(contracts_dir, sibling.version, selector.table_name)
                if history_file.exists():
                    continue
                read = read_sheet(wb, selector.table_name, merged.column_mapping)
                if read.error is not None:
                    print(
                        f"  (skip backfill v{sibling.version} {selector.table_name}: {read.error.message})",
                        file=sys.stderr,
                    )
                    continue
                rows = list(iter_field_rows(wb, read.spec))
                result = build_contract(
                    merged,
                    read.spec,
                    rows,
                    type_registry=registry,
                    spec_file_rel=str(spec_path).replace("\\", "/"),
                    table_name_from_config=selector.table_name,
                    allow_unknown_types=allow_unknown_types,
                )
                result = _enrich_with_keys(
                    result, keys_data, pk_index,
                    fk_allow_violations=settings.allow_foreign_key_violation,
                )
                result = _check_duplicate_table(result, selector.table_name, seen_tables)
                if isinstance(result, Rejection):
                    err_kinds = sorted({e.kind for e in result.errors})
                    print(
                        f"  (skip backfill v{sibling.version} {selector.table_name}: would reject - {','.join(err_kinds)})",
                        file=sys.stderr,
                    )
                    continue
                written = write_history_only(result, contracts_dir)
                print(f"  backfilled v{sibling.version} {selector.table_name} -> {_rel(written, contracts_dir)}")
                if settings.generate_drift:
                    _emit_drift_for_new_history(result, contracts_dir)
        finally:
            wb.close()


def _emit_joins(
    merged: MergedConfig,
    wb,
    contracts_by_table: dict[str, Contract],
    contracts_dir: Path,
    spec_file_rel: str,
    outcome: "_Outcome",
    *,
    write: bool,
    settings: Settings,
) -> JoinsContract | None:
    """Read the joins sheet, validate against generated contracts, and emit the
    joins artifact. Skipped if `merged.joins` is None (caller already checked).

    Returns the JoinsContract on success, or None on rejection — the caller uses
    this to thread joins data into the per-epic data dictionary.
    """
    assert merged.joins is not None
    joins_data = read_joins_sheet(wb, merged.joins)
    result = build_joins_result(
        version=merged.version,
        epic=merged.epic,
        spec_file_rel=spec_file_rel,
        spec_sheet=merged.joins.sheet_name,
        joins_data=joins_data,
        contracts_by_table=contracts_by_table,
    )

    if write:
        paths = write_joins_outputs(result, contracts_dir, write_history=settings.generate_history)
    else:
        paths = []

    if isinstance(result, JoinsContract):
        outcome.built += 1
        prefix = "[LINT-OK]" if not write else "[OK]"
        n = len(result.joins)
        if paths:
            canonical = next((p for p in paths if p.parent == contracts_dir), paths[0])
            history = next((p for p in paths if "history" in p.parts), None)
            suffix = f" (+ {_rel(history, contracts_dir)})" if history else ""
            print(f"{prefix} joins - {n} entries -> contracts/{_rel(canonical, contracts_dir)}{suffix}")
        else:
            print(f"{prefix} joins - {n} entries")
        return result
    outcome.rejected += 1
    prefix = "[LINT-REJECTED]" if not write else "[REJECTED]"
    if paths:
        print(f"{prefix} joins - {len(result.errors)} errors -> contracts/{_rel(paths[0], contracts_dir)}", file=sys.stderr)
    else:
        print(f"{prefix} joins - {len(result.errors)} errors", file=sys.stderr)
    return None


def _emit_result(
    result: Contract | Rejection,
    contracts_dir: Path,
    outcome: "_Outcome",
    *,
    write: bool,
    settings: Settings,
) -> None:
    """Apply outcome accounting + stdout reporting + (optionally) file writes
    for one build result. Unifies the four-way Contract/Rejection × write/lint branching.
    """
    if write:
        paths = write_outputs(result, contracts_dir, write_history=settings.generate_history)
    else:
        paths = []
    if isinstance(result, Contract):
        outcome.built += 1
        _print_success(result, paths, contracts_dir, lint=not write)
        if write and settings.generate_history and settings.generate_drift:
            _emit_drift_for_new_history(result, contracts_dir)
    else:
        outcome.rejected += 1
        _print_rejection(result.table, result.errors, paths, contracts_dir, lint=not write)


def _emit_drift_for_new_history(contract: Contract, contracts_dir: Path) -> None:
    """After a history snapshot is written, look for the nearest-older history
    version and write a drift file if one doesn't already exist."""
    prior = _find_prior_history(contracts_dir, contract.table, contract.version)
    if prior is None:
        return
    prior_path, prior_version = prior
    drift_file = drift_path_for(contracts_dir, contract.table, prior_version, contract.version)
    if drift_file.exists():
        return
    try:
        old_contract = Contract.load(prior_path)
    except Exception as e:
        print(f"  (skip drift {contract.table} v{prior_version}->v{contract.version}: cannot read prior: {e})", file=sys.stderr)
        return
    report = diff_contracts(old_contract, contract)
    if report.is_empty():
        return
    dump_yaml(drift_file, report.to_dict())
    _print_drift_summary(contract.table, prior_version, contract.version, report)


def _print_drift_summary(table: str, from_version: str, to_version: str, report: "DriftReport") -> None:
    s = report.summary()
    print(
        f"[DRIFT] {table} v{from_version} -> v{to_version}: "
        f"{s['breaking']} breaking, {s['additive']} additive, {s['cosmetic']} cosmetic"
    )


def _find_prior_history(contracts_dir: Path, table: str, current_version: str) -> tuple[Path, str] | None:
    history_root = contracts_dir / "history"
    if not history_root.is_dir():
        return None
    current_key = version_sort_key(current_version)
    best: tuple[tuple, Path, str] | None = None
    for version_dir in history_root.iterdir():
        if not version_dir.is_dir():
            continue
        v = version_dir.name
        if v == current_version:
            continue
        key = version_sort_key(v)
        if key >= current_key:
            continue
        candidate = version_dir / f"{table}.yaml"
        if not candidate.is_file():
            continue
        if best is None or key > best[0]:
            best = (key, candidate, v)
    if best is None:
        return None
    return best[1], best[2]


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
    # Imported lazily — keeps the validate-contract path off the generate import surface.
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
    _print_drift_summary(args.table, args.from_version, args.to_version, report)
    if args.write and not report.is_empty():
        out = drift_path_for(contracts_dir, args.table, args.from_version, args.to_version)
        dump_yaml(out, report.to_dict())
        print(f"  wrote {_rel(out, contracts_dir)}")
    return 0


def _resolve_table_selectors(merged: MergedConfig, wb) -> list[TableSelector]:
    if merged.tables == ALL_TABLES:
        return [TableSelector(table_name=s) for s in list_table_spec_sheets(wb, merged.column_mapping)]
    assert isinstance(merged.tables, list)
    return merged.tables


def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _print_success(c: Contract, paths: list[Path], contracts_dir: Path, *, lint: bool) -> None:
    prefix = "[LINT-OK]" if lint else "[OK]"
    if not paths:
        print(f"{prefix} {c.table} - {len(c.fields)} fields")
        return
    rels = [_rel(p, contracts_dir) for p in paths]
    canonical = next((r for r in rels if "/" not in r), rels[0])
    history = next((r for r in rels if r.startswith("history/")), None)
    suffix = f" (+ {history})" if history else ""
    print(f"{prefix} {c.table} - {len(c.fields)} fields -> contracts/{canonical}{suffix}")


def _print_rejection(table: str, errors: list, paths: list[Path], contracts_dir: Path, *, lint: bool) -> None:
    prefix = "[LINT-REJECTED]" if lint else "[REJECTED]"
    if not paths:
        print(f"{prefix} {table} - {len(errors)} errors", file=sys.stderr)
        return
    rel = _rel(paths[0], contracts_dir)
    print(f"{prefix} {table} - {len(errors)} errors -> contracts/{rel}", file=sys.stderr)
