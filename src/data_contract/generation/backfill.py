"""History backfill loop.

When `generate` runs on a new version, sibling version configs older than
the target with no history snapshot on disk get rebuilt so the
`contracts/history/` tree stays complete. Per-sibling failures are logged
and skipped; one bad sibling doesn't stop the rest.

Lives next to `pipeline.py` (rather than in cli.py) so the backfill
behaviour is testable without going through argparse.
"""

from __future__ import annotations

import sys
from pathlib import Path

from data_contract.errors import ConfigError, SpecReaderError
from data_contract.generation.builder import (
    build_contract,
    history_path_for_table,
    write_history_only,
)
from data_contract.generation.config import (
    Defaults,
    EpicConfig,
    discover_version_configs,
    merge,
    version_sort_key,
)
from data_contract.generation.spec_reader import (
    iter_field_rows,
    open_workbook,
    read_sheet,
)
from data_contract.generation.keys import build_pk_index, read_keys_sheet
from data_contract.settings import Settings
from data_contract.type_mapping import TypeRegistry


def backfill_missing_history(
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
    """Walk every sibling version config older than `target_version` and
    materialise any missing history snapshots.

    Errors that affect one sibling (bad config, missing spec, parser error,
    would-reject build) are logged and the sibling is skipped -- the loop
    moves on to the next.
    """
    from data_contract.generation.pipeline import (
        check_duplicate_table, emit_drift_for_new_history, enrich_with_keys,
    )
    from data_contract.contract import Rejection

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
            from data_contract.generation.pipeline import resolve_table_selectors

            tables = resolve_table_selectors(merged, wb)
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
                    merged, read.spec, rows,
                    type_registry=registry,
                    spec_file_rel=str(spec_path).replace("\\", "/"),
                    table_name_from_config=selector.table_name,
                    allow_unknown_types=allow_unknown_types,
                )
                result = enrich_with_keys(
                    result, keys_data, pk_index,
                    fk_allow_violations=settings.allow_foreign_key_violation,
                    allow_missing_primary_keys=settings.allow_missing_primary_keys,
                )
                result = check_duplicate_table(result, selector.table_name, seen_tables)
                if isinstance(result, Rejection):
                    err_kinds = sorted({e.kind for e in result.errors})
                    print(
                        f"  (skip backfill v{sibling.version} {selector.table_name}: would reject - {','.join(err_kinds)})",
                        file=sys.stderr,
                    )
                    continue
                written = write_history_only(result, contracts_dir)
                rel = _rel(written, contracts_dir)
                print(f"  backfilled v{sibling.version} {selector.table_name} -> {rel}")
                if settings.generate_drift:
                    emit_drift_for_new_history(result, contracts_dir)
        finally:
            wb.close()


def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")
