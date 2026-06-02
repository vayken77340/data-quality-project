from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from data_quality import __version__
from data_quality._util import dump_yaml, now_iso_z
from data_quality.config import (
    ALL_TABLES,
    DEFAULTS_FILENAME,
    Defaults,
    EpicConfig,
    MergedConfig,
    TableSelector,
    discover_version_configs,
    merge,
    select_version_config,
    version_sort_key,
)
from data_quality.contract import (
    Contract,
    Rejection,
    build_contract,
    drift_path_for,
    write_history_only,
    write_outputs,
)
from data_quality.drift import DriftReport, diff_contracts
from data_quality.errors import ConfigError, SpecReaderError
from data_quality.joins import (
    JoinsContract,
    JoinsRejection,
    build_joins_result,
    read_joins_sheet,
    write_joins_outputs,
)
from data_quality.keys import (
    KeysData,
    build_pk_index,
    enrich_field_contract_list,
    read_keys_sheet,
)
from data_quality.spec_reader import (
    iter_field_rows,
    list_table_spec_sheets,
    open_workbook,
    read_sheet,
)
from data_quality.type_mapping import TypeRegistry, load_type_registry


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
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data-quality",
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

    lint = sub.add_parser(
        "lint",
        help="Validate the spec end-to-end without writing any artifacts. Same exit codes as generate.",
    )
    _add_generate_args(lint)

    drift = sub.add_parser(
        "drift",
        help="Compute drift between two existing history snapshots without regenerating.",
    )
    drift.add_argument("--epic", required=True)
    drift.add_argument("--table", required=True)
    drift.add_argument("--from", dest="from_version", required=True, help="Older history version, e.g. 1.0.")
    drift.add_argument("--to", dest="to_version", required=True, help="Newer history version, e.g. 2.0.")
    drift.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    drift.add_argument("--write", action="store_true", help="Also write the drift YAML to contracts/drift/.")

    return p


def _add_generate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--epic", default=None, help="Epic name (e.g. 1118). If omitted, every epic under --epic-root is processed.")
    p.add_argument("--version", default=None, help="Pick a specific version config (mutually exclusive with --config).")
    p.add_argument("--config", default=None, help="Explicit path to a version config (requires --epic; mutually exclusive with --version).")
    p.add_argument("--epic-root", default=str(DEFAULT_EPIC_ROOT))
    p.add_argument("--types", default=str(DEFAULT_TYPES_PATH))
    p.add_argument("--allow-unknown-types", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")


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

    total = _Outcome()
    for epic in epics:
        outcome = _process_epic(
            epic=epic,
            epic_root=epic_root,
            registry=registry,
            version=args.version,
            explicit_config=Path(args.config) if args.config else None,
            allow_unknown_types=args.allow_unknown_types,
            backfill=backfill,
            write=write,
        )
        total.add(outcome)

    if len(epics) > 1:
        print(
            f"total: {total.built} built, {total.rejected} rejected"
            + (f", {len(total.epic_failures)} epic failure(s)" if total.epic_failures else "")
        )

    if total.epic_failures:
        return 1
    if total.rejected > 0:
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
    version: str | None,
    explicit_config: Path | None,
    allow_unknown_types: bool,
    backfill: bool,
    write: bool,
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
        defaults = Defaults.from_yaml(epic_configs_dir / DEFAULTS_FILENAME)
        merged = merge(defaults, epic_config)
    except ConfigError as e:
        print(f"config error in epic {epic}: {e}", file=sys.stderr)
        outcome.epic_failures.append(epic)
        return outcome

    print(f"using {merged.epic_config_path} ({reason})")

    if backfill:
        _backfill_missing_history(
            epic_dir=epic_dir,
            epic_configs_dir=epic_configs_dir,
            defaults=defaults,
            target_version=epic_config.version,
            target_config_path=epic_config.path,
            registry=registry,
            allow_unknown_types=allow_unknown_types,
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
            result = _enrich_with_keys(result, keys_data, pk_index)
            result = _check_duplicate_table(result, sheet_name, seen_tables)
            if isinstance(result, Contract):
                contracts_by_table[result.table] = result
            _emit_result(result, contracts_dir, outcome, write=write)

        if merged.joins is not None:
            _emit_joins(merged, wb, contracts_by_table, contracts_dir, spec_file_rel, outcome, write=write)
    finally:
        wb.close()

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
    from data_quality.errors import RejectionError

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
        from data_quality.errors import RejectionError
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

    _, errors = enrich_field_contract_list(contract.fields, contract.table, rows_for_table, pk_index)
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
                history_file = contracts_dir / "history" / selector.table_name / f"v{sibling.version}.yaml"
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
                result = _enrich_with_keys(result, keys_data, pk_index)
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
) -> None:
    """Read the joins sheet, validate against generated contracts, and emit the
    joins artifact. Skipped if `merged.joins` is None (caller already checked).
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
        paths = write_joins_outputs(result, contracts_dir)
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
    else:
        outcome.rejected += 1
        prefix = "[LINT-REJECTED]" if not write else "[REJECTED]"
        if paths:
            print(f"{prefix} joins - {len(result.errors)} errors -> contracts/{_rel(paths[0], contracts_dir)}", file=sys.stderr)
        else:
            print(f"{prefix} joins - {len(result.errors)} errors", file=sys.stderr)


def _emit_result(
    result: Contract | Rejection,
    contracts_dir: Path,
    outcome: "_Outcome",
    *,
    write: bool,
) -> None:
    """Apply outcome accounting + stdout reporting + (optionally) file writes
    for one build result. Unifies the four-way Contract/Rejection × write/lint branching.
    """
    paths = write_outputs(result, contracts_dir) if write else []
    if isinstance(result, Contract):
        outcome.built += 1
        _print_success(result, paths, contracts_dir, lint=not write)
        if write:
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


_HISTORY_VERSION_RE = re.compile(r"^v(?P<version>.+)\.yaml$")


def _find_prior_history(contracts_dir: Path, table: str, current_version: str) -> tuple[Path, str] | None:
    table_dir = contracts_dir / "history" / table
    if not table_dir.is_dir():
        return None
    current_key = version_sort_key(current_version)
    best: tuple[tuple, Path, str] | None = None
    for p in table_dir.iterdir():
        if not p.is_file():
            continue
        m = _HISTORY_VERSION_RE.match(p.name)
        if not m:
            continue
        v = m.group("version")
        if v == current_version:
            continue
        key = version_sort_key(v)
        if key >= current_key:
            continue
        if best is None or key > best[0]:
            best = (key, p, v)
    if best is None:
        return None
    return best[1], best[2]


def _cmd_drift(args: argparse.Namespace) -> int:
    epic_root = Path(args.epic_root)
    contracts_dir = epic_root / args.epic / "contracts"
    from_file = contracts_dir / "history" / args.table / f"v{args.from_version}.yaml"
    to_file = contracts_dir / "history" / args.table / f"v{args.to_version}.yaml"
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
