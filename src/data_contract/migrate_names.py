"""migrate-names: rewrite per-epic contract YAMLs to the v3 name layout.

Two rename rules + a materialization step apply on the same pass:

    V1 -> V2: each field block's ``source_name:`` -> ``extract_name:``.
    V2 -> V3: each field block's ``name:``         -> ``silver_name:``.
    Materialization: every field block must carry silver_name, extract_name,
    and bronze_name after migration. Missing extract_name defaults to
    silver_name's value; missing bronze_name defaults to silver_name's value
    (the tool has no spec-side bronze raw to default from extract).

After cutover (see plan), ``Contract.from_dict`` rejects both legacy keys
with actionable migrate-names hints AND requires all three name slots
present on every field. Same v2/v3 drift boundary applies as v1/v2 did:
drift workflows must run after the migration has been applied to history.
Subsequent ``generate --epic <E>`` runs re-derive bronze from the spec's
Nom Bronze column if present, producing a one-time bronze_name_changed
drift event for any field whose bronze cell differs from silver.

Uses ``yaml.safe_load`` + structured rewrite. Comments in the affected
YAMLs are not preserved (verified empty on the canonical four files).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from dq_core.yaml_io import FlowList, dump_yaml


# Per-field key renames applied on every migrate-names pass. Order matters:
# v1->v2 first (source_name -> extract_name) so the v2->v3 step sees the
# already-renamed field, though the two rules are on disjoint keys so the
# order is moot in practice.
_FIELD_KEY_RENAMES: dict[str, str] = {
    "source_name": "extract_name",  # v1 -> v2
    "name":        "silver_name",   # v2 -> v3
}


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
    """Apply every rule in ``_FIELD_KEY_RENAMES`` plus the always-emit
    materialization step to each field block.

    Returns the count of field rows mutated (renamed or materialized).
    Preserves key order on rename: rebuilds the field dict in-place rather
    than ``pop`` + reinsert. Materialised extract_name / bronze_name slots
    are inserted in the canonical position (right after silver_name) so
    output ordering matches what ``Contract.to_dict`` emits on a fresh
    generate. Idempotent: a re-run finds all three slots present and the
    legacy keys absent, makes no changes.
    """
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return 0
    count = 0
    for i, field_block in enumerate(fields):
        if not isinstance(field_block, dict):
            continue
        needs_rename = any(old in field_block for old in _FIELD_KEY_RENAMES)
        renamed = (
            _replace_keys_preserving_order(field_block, _FIELD_KEY_RENAMES)
            if needs_rename else dict(field_block)
        )
        materialised = _materialise_name_slots(renamed)
        if needs_rename or materialised is not renamed:
            fields[i] = materialised
            count += 1
    return count


def _replace_keys_preserving_order(d: dict, renames: dict[str, str]) -> dict:
    return {renames.get(k, k): v for k, v in d.items()}


def _materialise_name_slots(field_block: dict) -> dict:
    """Ensure every field block carries silver_name, extract_name, and
    bronze_name in canonical order. Missing extract_name / bronze_name
    default to silver_name's value. Returns the same dict if all three
    slots are already present (caller uses identity to detect a no-op).

    Canonical order: silver_name -> extract_name -> bronze_name -> rest.
    This matches ``FieldContract.to_dict``'s emission order so a migrated
    YAML looks identical to a freshly generated one regardless of the
    input block's original key order.
    """
    silver = field_block.get("silver_name")
    if silver is None:
        # No silver to default from -- leave alone; from_dict will surface the
        # missing-required-key error with the actionable migrate-names hint.
        return field_block
    if "extract_name" in field_block and "bronze_name" in field_block:
        return field_block

    out: dict = {
        "silver_name":  silver,
        "extract_name": field_block.get("extract_name", silver),
        "bronze_name":  field_block.get("bronze_name",  silver),
    }
    for k, v in field_block.items():
        if k not in ("silver_name", "extract_name", "bronze_name"):
            out[k] = v
    return out


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
