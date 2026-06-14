"""Per-epic generation pipeline.

Encapsulates everything that used to live inline in cli.py's
`_process_epic` -- reading the spec, building per-table contracts,
enriching with keys, emitting joins, writing outputs, printing summary
lines. The CLI is now a thin argparse + dispatch layer that calls
`process_epic` (or `run_generate_or_lint` for multi-epic loops).

Console output stays here (rather than in builder.py) so the build
pipeline modules stay quiet -- only the orchestration prints status.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from data_contract._util import dump_yaml, now_iso_z
from data_contract.contract import Contract, Rejection
from data_contract.errors import RejectionError, SpecReaderError
from data_contract.generation.builder import (
    build_contract,
    drift_path_for,
    write_outputs,
)
from data_contract.generation.config import (
    ALL_TABLES,
    Defaults,
    MergedConfig,
    SPECS_PARSING_FILENAME,
    TableSelector,
    merge,
    select_version_config,
    version_sort_key,
)
from data_contract.generation.docs import load_drift_entries, write_data_dictionary
from data_contract.generation.drift import DriftReport, diff_contracts
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
from data_contract.generation.spec_reader import (
    iter_field_rows,
    list_table_spec_sheets,
    open_workbook,
    read_sheet,
)
from data_contract.errors import ConfigError
from data_contract.settings import Settings
from data_contract.type_mapping import TypeRegistry


# ---------------------------------------------------------------------------
# Per-epic accumulator
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    built: int = 0
    rejected: int = 0
    epic_failures: list[str] = dc_field(default_factory=list)

    def add(self, other: "Outcome") -> None:
        self.built += other.built
        self.rejected += other.rejected
        self.epic_failures.extend(other.epic_failures)


# ---------------------------------------------------------------------------
# Per-epic processing
# ---------------------------------------------------------------------------


def process_epic(
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
    skip_self_check: bool,
    settings: Settings,
) -> Outcome:
    outcome = Outcome()
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
        from data_contract.generation.backfill import backfill_missing_history

        backfill_missing_history(
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
    selected = resolve_table_selectors(merged, wb)

    keys_data = read_keys_sheet(wb, merged.keys)
    pk_index = build_pk_index(keys_data.rows)

    seen_tables: dict[str, str] = {}
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
                emit_result(rejection, contracts_dir, outcome, write=write, settings=settings)
                continue

            sheet_spec = read.spec
            assert sheet_spec is not None
            rows = list(iter_field_rows(wb, sheet_spec))
            result = build_contract(
                merged, sheet_spec, rows,
                type_registry=registry,
                spec_file_rel=spec_file_rel,
                table_name_from_config=selector.table_name,
                allow_unknown_types=allow_unknown_types,
            )
            result = enrich_with_keys(
                result, keys_data, pk_index,
                fk_allow_violations=settings.allow_foreign_key_violation,
                allow_missing_primary_keys=settings.allow_missing_primary_keys,
            )
            result = check_duplicate_table(result, sheet_name, seen_tables)
            if isinstance(result, Contract):
                contracts_by_table[result.table] = result
            emit_result(result, contracts_dir, outcome, write=write, settings=settings)

        joins_contract: JoinsContract | None = None
        if merged.joins is not None and settings.generate_join_contract:
            joins_contract = emit_joins(
                merged, wb, contracts_by_table, contracts_dir, spec_file_rel, outcome,
                write=write, settings=settings,
            )
    finally:
        wb.close()

    if write and not skip_self_check and contracts_by_table:
        self_check_post_build(contracts_by_table, joins_contract, registry, outcome)

    if write and contracts_by_table:
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


# ---------------------------------------------------------------------------
# Build-result transformations (Contract | Rejection -> Contract | Rejection)
# ---------------------------------------------------------------------------


def check_duplicate_table(
    result: Contract | Rejection,
    sheet_name: str,
    seen_tables: dict[str, str],
) -> Contract | Rejection:
    """If `result.table` was already produced by an earlier sheet, convert it
    into a duplicate-table rejection and route to a disambiguated filename so
    the earlier sheet's canonical/history output isn't overwritten."""
    prior_sheet = seen_tables.get(result.table)
    if prior_sheet is None:
        seen_tables[result.table] = sheet_name
        return result

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


def enrich_with_keys(
    result: Contract | Rejection,
    keys_data: KeysData,
    pk_index: dict[str, set[str]],
    *,
    fk_allow_violations: bool = False,
    allow_missing_primary_keys: bool = False,
) -> Contract | Rejection:
    """Apply keys-sheet PK/FK enrichment to a fresh build result.

    - If `result` is already a Rejection: prepend any keys-sheet structural
      errors so reviewers see the upstream cause.
    - If `result` is a Contract and the keys-sheet had structural errors:
      convert to a Rejection carrying those errors.
    - If `result` is a Contract and the table has no row in the keys sheet:
      * `allow_missing_primary_keys=True` -> return the contract unchanged
        (no PK enrichment; pk_uniqueness will have nothing to check, so
        duplicates in the sample become tolerated).
      * Otherwise -> Rejection (keys_missing_table).
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
            version=contract.version, epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file, spec_sheet=contract.spec_sheet,
            table=contract.table, errors=list(keys_data.errors),
        )

    rows_for_table = keys_data.rows_for_table(contract.table)
    if not rows_for_table:
        if allow_missing_primary_keys:
            print(
                f"[WARN] {contract.table}: no row in the keys sheet; "
                f"emitting the contract without a primary key "
                f"(allow_missing_primary_keys=true)",
                file=sys.stderr,
            )
            return contract
        return Rejection(
            version=contract.version, epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file, spec_sheet=contract.spec_sheet,
            table=contract.table,
            errors=[RejectionError(
                kind="keys_missing_table", field="table_name", value=contract.table,
                message=(
                    f"keys sheet has no row for table {contract.table!r}; "
                    f"every generated table must have an entry in the keys sheet "
                    f"(set allow_missing_primary_keys=true in .env to relax)"
                ),
            )],
        )

    _, errors, fk_warnings = enrich_field_contract_list(
        contract.fields, contract.table, rows_for_table, pk_index,
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
            version=contract.version, epic=contract.epic,
            generated_at=contract.generated_at,
            spec_file=contract.spec_file, spec_sheet=contract.spec_sheet,
            table=contract.table, errors=errors,
        )
    return contract


# ---------------------------------------------------------------------------
# Self-check + joins emission
# ---------------------------------------------------------------------------


def self_check_post_build(
    contracts_by_table: dict[str, Contract],
    joins_contract: JoinsContract | None,
    type_registry: TypeRegistry,
    outcome: Outcome,
) -> None:
    """Post-build invariant pass over every freshly emitted contract + joins.

    Mirrors what `validate-contract` would catch on the on-disk YAMLs, but
    runs against the in-memory dataclasses so emission bugs are caught at
    the source. Errors go to stderr; the affected table is bumped onto
    `outcome.rejected`. Canonical YAMLs are NOT deleted -- left in place
    for the human to inspect.
    """
    from data_contract.generation.validate_contract import (
        check_invariants, check_joins_invariants,
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
        errors = check_joins_invariants(joins_contract.to_dict(), peer_field_names)
        if errors:
            outcome.rejected += 1
            print(f"[SELF-CHECK-FAIL] joins - {len(errors)} invariant errors", file=sys.stderr)
            for err in errors:
                print(f"  - {err.render()}", file=sys.stderr)


def emit_joins(
    merged: MergedConfig,
    wb,
    contracts_by_table: dict[str, Contract],
    contracts_dir: Path,
    spec_file_rel: str,
    outcome: Outcome,
    *,
    write: bool,
    settings: Settings,
) -> JoinsContract | None:
    """Read the joins sheet, validate against generated contracts, and emit
    the joins artifact. Skipped if `merged.joins` is None.

    Returns the JoinsContract on success, or None on rejection.
    """
    assert merged.joins is not None
    joins_data = read_joins_sheet(wb, merged.joins)
    result = build_joins_result(
        version=merged.version, epic=merged.epic, spec_file_rel=spec_file_rel,
        spec_sheet=merged.joins.sheet_name,
        joins_data=joins_data, contracts_by_table=contracts_by_table,
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


# ---------------------------------------------------------------------------
# Result emission (outcome bookkeeping + console reporting + write)
# ---------------------------------------------------------------------------


def emit_result(
    result: Contract | Rejection,
    contracts_dir: Path,
    outcome: Outcome,
    *,
    write: bool,
    settings: Settings,
) -> None:
    """Unify the four-way Contract/Rejection x write/lint branching."""
    if write:
        paths = write_outputs(result, contracts_dir, write_history=settings.generate_history)
    else:
        paths = []
    if isinstance(result, Contract):
        outcome.built += 1
        _print_success(result, paths, contracts_dir, lint=not write)
        if write and settings.generate_history and settings.generate_drift:
            emit_drift_for_new_history(result, contracts_dir)
    else:
        outcome.rejected += 1
        _print_rejection(result.table, result.errors, paths, contracts_dir, lint=not write)


def emit_drift_for_new_history(contract: Contract, contracts_dir: Path) -> None:
    """After a history snapshot is written, look for the nearest-older history
    version and write a drift file if one doesn't already exist."""
    prior = find_prior_history(contracts_dir, contract.table, contract.version)
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
    print_drift_summary(contract.table, prior_version, contract.version, report)


def print_drift_summary(table: str, from_version: str, to_version: str, report: DriftReport) -> None:
    s = report.summary()
    print(
        f"[DRIFT] {table} v{from_version} -> v{to_version}: "
        f"{s['breaking']} breaking, {s['additive']} additive, {s['cosmetic']} cosmetic"
    )


def find_prior_history(contracts_dir: Path, table: str, current_version: str) -> tuple[Path, str] | None:
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


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def resolve_table_selectors(merged: MergedConfig, wb) -> list[TableSelector]:
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
