"""migrate-names: rewrite per-epic contract YAMLs to the v2 name layout.

V1 (today): each field block uses ``source_name:`` for the raw extract header.
V2 (target): renamed to ``extract_name:`` plus an optional ``bronze_name:``.

One-shot tool. After cutover (see plan), ``Contract.from_dict`` rejects the
v1 ``source_name:`` key. Bronze name is not invented here; operators add
``bronze_name:`` manually or re-run ``generate`` after editing the spec.

Uses ``yaml.safe_load`` + structured rewrite. Comments in the affected
YAMLs are not preserved (verified empty on the canonical four files).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from dq_core.yaml_io import FlowList, dump_yaml


OLD_KEY = "source_name"
NEW_KEY = "extract_name"


@dataclass
class FileResult:
    path: Path
    changed: bool
    fields_renamed: int


@dataclass
class MigrationReport:
    files_visited: list[FileResult] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def files_changed(self) -> int:
        return sum(1 for f in self.files_visited if f.changed)

    @property
    def fields_renamed(self) -> int:
        return sum(f.fields_renamed for f in self.files_visited)


def migrate_epic(
    epic_id: str,
    epic_root: Path = Path("epics"),
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Migrate every contract YAML under ``epics/<epic_id>/contracts/``."""
    contracts_dir = epic_root / epic_id / "contracts"
    if not contracts_dir.is_dir():
        report = MigrationReport()
        report.errors.append((contracts_dir, f"not a directory: {contracts_dir}"))
        return report
    return _run(_walk_contracts(contracts_dir), dry_run=dry_run)


def migrate_path(path: Path, *, dry_run: bool = False) -> MigrationReport:
    """Migrate every ``*.yaml`` under ``path`` (recursive)."""
    if not path.is_dir():
        report = MigrationReport()
        report.errors.append((path, f"not a directory: {path}"))
        return report
    return _run(sorted(path.rglob("*.yaml")), dry_run=dry_run)


def migrate_all(
    epic_root: Path = Path("epics"),
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Migrate every epic under ``epic_root`` whose ``contracts/`` exists."""
    if not epic_root.is_dir():
        report = MigrationReport()
        report.errors.append((epic_root, f"not a directory: {epic_root}"))
        return report
    files: list[Path] = []
    for child in sorted(epic_root.iterdir()):
        contracts_dir = child / "contracts"
        if contracts_dir.is_dir():
            files.extend(_walk_contracts(contracts_dir))
    return _run(files, dry_run=dry_run)


def _walk_contracts(contracts_dir: Path) -> list[Path]:
    """Yield current + history contract YAMLs under ``contracts_dir``."""
    current = sorted(contracts_dir.glob("*.yaml"))
    history = sorted((contracts_dir / "history").rglob("*.yaml")) if (contracts_dir / "history").is_dir() else []
    return current + history


def _run(paths: Iterable[Path], *, dry_run: bool) -> MigrationReport:
    """Two-pass: parse + mutate everything in memory; only write if all parsed.

    Plan calls for no half-migrate: on any parse error we report all errors
    and write nothing. With ~4 canonical files this is cheap.
    """
    report = MigrationReport()
    pending: list[tuple[Path, dict, int]] = []
    for path in paths:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            report.errors.append((path, f"parse error: {e}"))
            continue
        if not isinstance(payload, dict):
            report.errors.append((path, "top-level YAML must be a mapping"))
            continue
        renamed = _rename_field_keys(payload)
        pending.append((path, payload, renamed))
    if report.errors:
        return report
    for path, payload, renamed in pending:
        changed = renamed > 0
        report.files_visited.append(FileResult(path=path, changed=changed, fields_renamed=renamed))
        if changed and not dry_run:
            _restore_flow_styles(payload)
            dump_yaml(path, payload)
    return report


def _rename_field_keys(payload: dict) -> int:
    """Rename ``source_name`` -> ``extract_name`` in every field block.

    Returns the number of field rows rewritten. Preserves key order by
    rebuilding the field dict in-place rather than ``pop`` + reinsert
    (which would move the key to the end).
    """
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return 0
    count = 0
    for i, field_block in enumerate(fields):
        if not isinstance(field_block, dict) or OLD_KEY not in field_block:
            continue
        fields[i] = _replace_key_preserving_order(field_block, OLD_KEY, NEW_KEY)
        count += 1
    return count


def _replace_key_preserving_order(d: dict, old: str, new: str) -> dict:
    return {(new if k == old else k): v for k, v in d.items()}


def _restore_flow_styles(payload: dict) -> None:
    """Re-wrap ``data_values`` token lists as ``FlowList`` so the dumper
    keeps them inline. yaml.safe_load returns plain lists, which would
    otherwise expand to block style and pollute the migration diff."""
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return
    for field_block in fields:
        if not isinstance(field_block, dict):
            continue
        data_values = field_block.get("data_values")
        if not isinstance(data_values, dict):
            continue
        for k, v in list(data_values.items()):
            if isinstance(v, list) and not isinstance(v, FlowList):
                data_values[k] = FlowList(v)


def format_report(report: MigrationReport, *, dry_run: bool) -> Iterator[str]:
    """Human-readable lines describing the run; one line per file plus a summary."""
    verb = "would migrate" if dry_run else "migrated"
    for r in report.files_visited:
        if r.changed:
            yield f"[MIGRATE] {verb} {r.path} ({r.fields_renamed} field(s) renamed)"
        else:
            yield f"[MIGRATE] {r.path} (unchanged)"
    for path, msg in report.errors:
        yield f"[MIGRATE-ERROR] {path}: {msg}"
    yield (
        f"migrate-names: visited {len(report.files_visited)} file(s), "
        f"{report.files_changed} changed, "
        f"{report.fields_renamed} field(s) renamed, "
        f"{len(report.errors)} error(s)"
    )
