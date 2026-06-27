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

from data_contract._util import now_iso_z
from data_contract.contract import Contract, Rejection
from data_contract.errors import RejectionError, SpecReaderError
from data_contract.generation.builder import (
    build_contract,
    history_path_for_table,
    write_history_only,
    write_outputs,
)
from data_contract.generation.config import (
    ALL_TABLES,
    Defaults,
    EpicConfig,
    MergedConfig,
    SPECS_PARSING_FILENAME,
    TableSelector,
    discover_version_configs,
    merge,
    version_sort_key,
)
from data_contract.generation.docs import load_drift_entries, write_data_dictionary
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
from data_contract.generation.validate_contract import check_invariants_in_memory
from data_contract.errors import ConfigError
from data_contract.settings import Settings
from data_contract.targets import load_target_config, resolve_target_path
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
    write: bool,
    skip_self_check: bool,
    settings: Settings,
) -> Outcome:
    """Build contracts for an epic.

    Default behaviour (no `--version`, no `--config`): every version config
    under `epics/<E>/configs/contracts/` is built. The highest version becomes
    canonical (writes to `contracts/<table>.yaml` AND
    `contracts/history/<v>/<table>.yaml`); older versions write history only.

    `--version <x>`: builds only x. If x is the highest known version, the
    canonical pair is written; otherwise only `history/<x>/` is touched.
    `--config <path>`: same single-version behaviour, file resolved by path.

    Drift is no longer emitted here -- use `generate-drift` for that.
    """
    outcome = Outcome()
    epic_dir = epic_root / epic
    epic_configs_dir = epic_dir / "configs"

    print(f"--- epic {epic} ---")

    try:
        all_configs, canonical_version, build_configs = _resolve_build_set(
            epic_configs_dir,
            version=version,
            explicit_config=explicit_config,
        )
        defaults = Defaults.from_yaml(specs_parsing_path)
    except ConfigError as e:
        print(f"config error in epic {epic}: {e}", file=sys.stderr)
        outcome.epic_failures.append(epic)
        return outcome

    canonical_cfg = next(c for c in all_configs if c.version == canonical_version)
    registry = _overlay_target_or_warn(
        registry, epic=epic, canonical_cfg=canonical_cfg,
        epic_dir=epic_dir, specs_parsing_path=specs_parsing_path,
    )

    canonical_contracts: dict[str, Contract] = {}
    canonical_joins: JoinsContract | None = None
    canonical_spec_file_rel: str | None = None
    canonical_merged: MergedConfig | None = None
    # Explicit selection (`--version` / `--config`) means the user asked for
    # this exact version -- they expect a rebuild even if history exists.
    # The implicit "build all" default keeps the "skip existing history"
    # safety net so a stray run doesn't wipe a hand-curated older snapshot.
    explicit_selection = len(build_configs) < len(all_configs) or len(all_configs) == 1 and version is not None

    for cfg in build_configs:
        try:
            merged = merge(defaults, cfg)
        except ConfigError as e:
            print(f"  (skip v{cfg.version}: {e})", file=sys.stderr)
            outcome.rejected += 1
            continue

        is_canonical = cfg.version == canonical_version
        result = _build_one_version(
            merged=merged, epic_dir=epic_dir, registry=registry,
            allow_unknown_types=allow_unknown_types,
            settings=settings, outcome=outcome,
            write=write, is_canonical=is_canonical,
            force_rebuild=explicit_selection,
        )
        if is_canonical and result is not None:
            canonical_contracts, canonical_joins, canonical_spec_file_rel = result
            canonical_merged = merged

    if write and not skip_self_check and canonical_contracts:
        self_check_post_build(canonical_contracts, canonical_joins, registry, outcome)

    if write and canonical_contracts and canonical_merged is not None and canonical_spec_file_rel is not None:
        contracts_dir = epic_dir / "contracts"
        drift_aggregate = load_drift_entries(contracts_dir)
        docs_path = write_data_dictionary(
            epic_dir=epic_dir,
            epic=canonical_merged.epic,
            version=canonical_merged.version,
            spec_file=canonical_spec_file_rel,
            contracts=list(canonical_contracts.values()),
            joins=canonical_joins,
            drift=drift_aggregate,
        )
        print(f"[DOCS] {_rel(docs_path, epic_dir)}")

    print(f"epic {epic}: {outcome.built} built, {outcome.rejected} rejected")
    return outcome


def _resolve_build_set(
    epic_configs_dir: Path,
    *,
    version: str | None,
    explicit_config: Path | None,
) -> tuple[list[EpicConfig], str, list[EpicConfig]]:
    """Pick `all_configs`, `canonical_version`, and the subset to build.

    Returns `(all_configs, canonical_version, build_configs)`:
      - `all_configs` is every discovered version config in the subdir, parsed.
        Used to determine the canonical (highest) version, even when only a
        subset is being built so the canonical-vs-history routing is correct.
      - `canonical_version` is the highest version among `all_configs`. The
        config matching it gets written to `contracts/<table>.yaml`.
      - `build_configs` is the subset actually being built this run.

    `--config <path>` and `--version <x>` both narrow `build_configs` to a
    single entry; the canonical version is still computed from the FULL set
    so passing `--version <older>` writes only history, not canonical.
    """
    if explicit_config is not None and version is not None:
        raise ConfigError("--version and --config are mutually exclusive")

    if explicit_config is not None:
        if not explicit_config.is_file():
            raise ConfigError(f"explicit config not found: {explicit_config}")
        explicit = EpicConfig.from_yaml(explicit_config)
        # Still discover siblings so `canonical_version` covers them; an
        # `--config` of an older sibling stays history-only.
        all_configs = _load_all_configs(epic_configs_dir, fallback=explicit)
        canonical_version = _pick_canonical(all_configs)
        print(f"using {explicit_config} (explicit path)")
        return all_configs, canonical_version, [explicit]

    all_configs = _load_all_configs(epic_configs_dir, fallback=None)
    canonical_version = _pick_canonical(all_configs)

    if version is not None:
        wanted = version.strip()
        matches = [c for c in all_configs if c.version == wanted]
        if not matches:
            raise ConfigError(
                f"no config with version {wanted!r}; "
                f"available: {[c.version for c in all_configs]}"
            )
        if len(matches) > 1:
            raise ConfigError(
                f"multiple configs with version {wanted!r}: "
                f"{[str(c.path) for c in matches]}"
            )
        print(f"using {matches[0].path} (version {wanted})")
        return all_configs, canonical_version, matches

    print(
        f"building all versions ({', '.join(c.version for c in all_configs)}); "
        f"canonical = {canonical_version}"
    )
    return all_configs, canonical_version, list(all_configs)


def _load_all_configs(
    epic_configs_dir: Path, *, fallback: EpicConfig | None,
) -> list[EpicConfig]:
    """Parse every discoverable version config. `fallback` covers the
    `--config <path>` case where the explicit file might be outside the
    subdir -- we still want it in the set so canonical-detection sees it."""
    paths = discover_version_configs(epic_configs_dir)
    configs = [EpicConfig.from_yaml(p) for p in paths]
    if fallback is not None and not any(c.path == fallback.path for c in configs):
        configs.append(fallback)
    if not configs:
        raise ConfigError(f"no version configs found under {epic_configs_dir}")
    return configs


def _pick_canonical(configs: list[EpicConfig]) -> str:
    return max(configs, key=lambda c: version_sort_key(c.version)).version


def _overlay_target_or_warn(
    registry: TypeRegistry,
    *,
    epic: str,
    canonical_cfg: EpicConfig,
    epic_dir: Path,
    specs_parsing_path: Path,
) -> TypeRegistry:
    """Overlay the canonical version's target onto `registry`, or warn + return
    the input registry unchanged.

    Per-epic target files (epics/<E>/configs/targets/<n>.yaml) beat the
    repo-root file. Picks the target from the canonical (highest) version --
    targets are an epic-wide attribute, not a per-version one in practice.

    Soft failure: when the target file is missing or malformed, generation
    proceeds without the overlay -- `physical_type` is omitted from every
    field but the contract is still valid. Validation is the strict consumer.
    """
    try:
        target_path = resolve_target_path(
            canonical_cfg.target,
            repo_root=specs_parsing_path.parent.parent,
            epic_dir=epic_dir,
        )
        target_config = load_target_config(target_path)
        return registry.with_target(target_config)
    except ConfigError as e:
        print(
            f"[WARN] epic {epic}: target overlay unavailable ({e}); "
            f"contracts will be emitted without `physical_type`",
            file=sys.stderr,
        )
        return registry


def _build_one_version(
    *,
    merged: MergedConfig,
    epic_dir: Path,
    registry: TypeRegistry,
    allow_unknown_types: bool,
    settings: Settings,
    outcome: Outcome,
    write: bool,
    is_canonical: bool,
    force_rebuild: bool = False,
) -> tuple[dict[str, Contract], JoinsContract | None, str] | None:
    """Build every table for one version and emit results.

    For the canonical version, writes both `contracts/<table>.yaml` and
    `contracts/history/<v>/<table>.yaml`. For older versions, only the
    history snapshot is written -- canonical is left alone so an older
    backfill never rolls back the latest.
    """
    spec_path = epic_dir / "specs" / merged.spec_file_name
    contracts_dir = epic_dir / "contracts"

    try:
        wb = open_workbook(spec_path)
    except SpecReaderError as e:
        print(f"  (skip v{merged.version}: {e})", file=sys.stderr)
        outcome.rejected += 1
        return None

    spec_file_rel = str(spec_path).replace("\\", "/")

    try:
        selected = resolve_table_selectors(merged, wb)
        keys_data = read_keys_sheet(wb, merged.keys)
        pk_index = build_pk_index(keys_data.rows)
        seen_tables: dict[str, str] = {}
        version_contracts: dict[str, Contract] = {}

        for selector in selected:
            # Skip silently when an older version's history already exists --
            # backfill semantics: the implicit "build all" loop never
            # overwrites a hand-curated older snapshot. Explicit
            # `--version <x>` (force_rebuild) always rebuilds.
            if not is_canonical and not force_rebuild:
                history_file = history_path_for_table(
                    contracts_dir, merged.version, selector.table_name,
                )
                if history_file.exists():
                    continue

            result = build_one_table(
                merged, selector, wb,
                registry=registry,
                spec_file_rel=spec_file_rel,
                allow_unknown_types=allow_unknown_types,
                keys_data=keys_data,
                pk_index=pk_index,
                seen_tables=seen_tables,
                settings=settings,
            )
            if isinstance(result, Contract):
                version_contracts[result.table] = result
            _emit_version_result(
                result, contracts_dir, outcome,
                write=write, is_canonical=is_canonical, version=merged.version,
            )

        joins_contract: JoinsContract | None = None
        if is_canonical and merged.joins is not None and settings.generate_join_contract:
            joins_contract = emit_joins(
                merged, wb, version_contracts, contracts_dir, spec_file_rel, outcome,
                write=write, settings=settings,
            )
    finally:
        wb.close()

    if is_canonical:
        return version_contracts, joins_contract, spec_file_rel
    return None


# ---------------------------------------------------------------------------
# Build-result transformations (Contract | Rejection -> Contract | Rejection)
# ---------------------------------------------------------------------------


def build_one_table(
    merged: MergedConfig,
    selector: TableSelector,
    wb,
    *,
    registry: TypeRegistry,
    spec_file_rel: str,
    allow_unknown_types: bool,
    keys_data: KeysData,
    pk_index: dict[str, set[str]],
    seen_tables: dict[str, str],
    settings: Settings,
) -> Contract | Rejection:
    """Build one table end-to-end: read sheet, build contract, enrich with keys,
    check for cross-sheet duplicates. Returns the final `Contract` on success
    or a `Rejection` carrying every collected error.

    Pure: no file writes, no console prints. `process_epic`'s per-version
    loop calls this once per (version, table); the caller chooses how to
    persist or report the result.
    """
    sheet_name = selector.table_name
    read = read_sheet(wb, sheet_name, merged.column_mapping)
    if read.error is not None:
        return Rejection(
            version=merged.version,
            epic=merged.epic,
            generated_at=now_iso_z(),
            spec_file=spec_file_rel,
            spec_sheet=sheet_name,
            table=sheet_name,
            target=merged.target,
            errors=keys_data.errors + [read.error],
        )

    sheet_spec = read.spec
    assert sheet_spec is not None
    rows = list(iter_field_rows(wb, sheet_spec))
    result = build_contract(
        merged, sheet_spec, rows,
        type_registry=registry,
        spec_file_rel=spec_file_rel,
        table_name_from_config=sheet_name,
        allow_unknown_types=allow_unknown_types,
    )
    result = enrich_with_keys(
        result, keys_data, pk_index,
        fk_allow_violations=settings.allow_foreign_key_violation,
        allow_missing_primary_keys=settings.allow_missing_primary_keys,
    )
    result = check_duplicate_table(result, sheet_name, seen_tables)
    return result


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
        target=result.target,
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
            table=contract.table, target=contract.target,
            errors=list(keys_data.errors),
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
            table=contract.table, target=contract.target,
            errors=[RejectionError(
                kind="keys_missing_table", field="table_name", value=contract.table,
                message=(
                    f"keys sheet has no row for table {contract.table!r}; "
                    f"every generated table must have an entry in the keys sheet "
                    f"(set allow_missing_primary_keys=true in .env to relax)"
                ),
            )],
        )

    enriched_fields, errors, fk_warnings = enrich_field_contract_list(
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
            table=contract.table, target=contract.target, errors=errors,
        )
    # `FieldContract` is frozen; enrichment returns new instances. Rebind the
    # contract's field list to the enriched copies before returning.
    contract.fields = enriched_fields
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
    result = check_invariants_in_memory(contracts_by_table, joins_contract, type_registry)

    for table, errors in result.per_table.items():
        if errors:
            outcome.rejected += 1
            print(f"[SELF-CHECK-FAIL] {table} - {len(errors)} invariant errors", file=sys.stderr)
            for err in errors:
                print(f"  - {err.render()}", file=sys.stderr)

    if result.joins:
        outcome.rejected += 1
        print(f"[SELF-CHECK-FAIL] joins - {len(result.joins)} invariant errors", file=sys.stderr)
        for err in result.joins:
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


def _emit_version_result(
    result: Contract | Rejection,
    contracts_dir: Path,
    outcome: Outcome,
    *,
    write: bool,
    is_canonical: bool,
    version: str,
) -> None:
    """Per-version writer. Canonical = `write_outputs` (canonical + history);
    older = `write_history_only`. Rejections still surface via `write_outputs`'s
    rejected/ path regardless of canonical-ness so a bad spec at any version
    leaves a record."""
    paths: list[Path] = []
    if write:
        if is_canonical:
            paths = write_outputs(result, contracts_dir)
        elif isinstance(result, Contract):
            paths = [write_history_only(result, contracts_dir)]
        else:
            # Non-canonical rejection: route through write_outputs so the
            # rejected file still lands -- but it'll be under the same
            # rejected/<table>.yaml location and could collide with a
            # canonical rejection. Tag it with the version so older-version
            # failures don't blow away the latest's rejection record.
            paths = write_outputs(result, contracts_dir)
    if isinstance(result, Contract):
        outcome.built += 1
        _print_success(result, paths, contracts_dir, lint=not write, version_tag=None if is_canonical else version)
    else:
        outcome.rejected += 1
        _print_rejection(
            result.table, result.errors, paths, contracts_dir,
            lint=not write, version_tag=None if is_canonical else version,
        )


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


def _print_success(
    c: Contract, paths: list[Path], contracts_dir: Path, *,
    lint: bool, version_tag: str | None = None,
) -> None:
    prefix = "[LINT-OK]" if lint else "[OK]"
    tag = f" v{version_tag}" if version_tag else ""
    if not paths:
        print(f"{prefix} {c.table}{tag} - {len(c.fields)} fields")
        return
    rels = [_rel(p, contracts_dir) for p in paths]
    canonical = next((r for r in rels if "/" not in r), None)
    history = next((r for r in rels if r.startswith("history/")), None)
    # Canonical write: "OK table -> contracts/<table>.yaml (+ history/v/<table>.yaml)"
    # History-only write (older version): "OK table v1.0 -> contracts/history/1.0/<table>.yaml"
    if canonical is not None:
        suffix = f" (+ {history})" if history else ""
        print(f"{prefix} {c.table}{tag} - {len(c.fields)} fields -> contracts/{canonical}{suffix}")
    else:
        print(f"{prefix} {c.table}{tag} - {len(c.fields)} fields -> contracts/{history or rels[0]}")


def _print_rejection(
    table: str, errors: list, paths: list[Path], contracts_dir: Path, *,
    lint: bool, version_tag: str | None = None,
) -> None:
    prefix = "[LINT-REJECTED]" if lint else "[REJECTED]"
    tag = f" v{version_tag}" if version_tag else ""
    if not paths:
        print(f"{prefix} {table}{tag} - {len(errors)} errors", file=sys.stderr)
        return
    rel = _rel(paths[0], contracts_dir)
    print(f"{prefix} {table}{tag} - {len(errors)} errors -> contracts/{rel}", file=sys.stderr)
