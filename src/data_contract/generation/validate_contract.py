"""Contract-on-disk validation.

`validate-contract` loads YAML contracts that already exist on disk and runs
three passes:

1. **Syntactic** via `schema_export.validate_against_schema` — JSON Schema
   against the published contract schema.
2. **Semantic invariants** — cross-field / cross-table checks the schema can't
   express (PK not nullable, max_length only on strings, FK targets exist,
   etc.).
3. **Path-consistency** — for files in an `epics/<E>/contracts/[...]` tree,
   the YAML's `epic` (and `version` for history snapshots) must match the
   location on disk.

Also validates `joins.yaml` (semantic only): source/target tables exist,
source/target columns exist on the referenced contracts, join type and
cardinality are recognized.

Intentionally separate from `lint`: `lint` re-runs the spec → contract build
and validates the inputs; `validate-contract` accepts a YAML file as the input
and validates it. The downstream data validator uses this as its precondition
guard. The same `check_invariants` engine is reused by `generate`'s post-build
self-check.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

from dq_core.contract import Contract
from dq_core.epic import discover_epics
from dq_core.yaml_io import load_yaml_mapping
from dq_core.errors import ConfigError
from data_contract.generation.invariants import (
    InvariantError,
    check_invariants,
    check_joins_invariants,
    check_path_invariants,
)
from data_contract.generation.schema_export import validate_against_schema
from dq_core.type_mapping import TypeRegistry, load_type_registry


class ValidateContractAbort(Exception):
    """Top-level abort signal. Caught at the entry point of
    `run_validate_contract`, formatted via `_emit_init_error`, returned as
    exit code 1. Use for "give up before any contract is validated" errors
    (mutual-exclusion args, type registry missing, no epics found, etc.) so
    the orchestrator reads as one happy path under one catch.
    """


@dataclass
class ValidationOutcome:
    scope: str            # e.g. "epic:1118" or "file:/tmp/x.yaml"
    table: str            # the contract's `table` field, or "joins" for joins
    schema_errors: list[str] = dc_field(default_factory=list)
    invariant_errors: list[InvariantError] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.schema_errors and not self.invariant_errors

    @property
    def n_errors(self) -> int:
        return len(self.schema_errors) + len(self.invariant_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "table": self.table,
            "ok": self.ok,
            "schema_errors": list(self.schema_errors),
            "invariant_errors": [e.to_dict() for e in self.invariant_errors],
        }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_validate_contract(
    *,
    epic: str | None,
    file: str | None,
    epic_root: Path,
    types_path: Path,
    allow_unknown_constraints: bool,
    output_format: str = "text",
) -> int:
    """CLI entry point. Returns exit code (0 / 1 / 2)."""
    outcomes: list[ValidationOutcome] = []
    epic_failed = False

    try:
        if file is not None and epic is not None:
            raise ValidateContractAbort("--file and --epic are mutually exclusive")

        try:
            type_registry = load_type_registry(types_path)
        except ConfigError as e:
            raise ValidateContractAbort(f"config error: {e}") from e

        if file is not None:
            path = Path(file)
            loaded = _load_yaml_safely(path, output_format)
            if loaded is None:
                return 1
            outcomes.append(_validate_one_payload(
                payload=loaded, path=path, scope=f"file:{path}",
                type_registry=type_registry,
                peer_field_names={},
                allow_unknown_constraints=allow_unknown_constraints,
            ))
        else:
            epics = [epic] if epic is not None else discover_epics(
                epic_root, required_subdir="contracts",
            )
            if not epics:
                raise ValidateContractAbort(f"no epics found under {epic_root}")
            for e in epics:
                ep_outcomes, ep_failed = _validate_epic(
                    epic_dir=epic_root / e,
                    type_registry=type_registry,
                    allow_unknown_constraints=allow_unknown_constraints,
                    output_format=output_format,
                )
                outcomes.extend(ep_outcomes)
                if ep_failed:
                    epic_failed = True
    except ValidateContractAbort as exc:
        _emit_init_error(str(exc), output_format)
        return 1

    _emit_outcomes(outcomes, output_format)
    if epic_failed:
        return 1
    return 2 if any(not o.ok for o in outcomes) else 0


def _emit_init_error(message: str, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"ok": False, "error": message}))
    else:
        print(f"validate-contract: {message}", file=sys.stderr)


def _emit_outcomes(outcomes: list[ValidationOutcome], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({
            "ok": all(o.ok for o in outcomes),
            "results": [o.to_dict() for o in outcomes],
        }, indent=2))
        return

    last_scope: str | None = None
    for o in outcomes:
        # Print a header line once per scope change.
        if o.scope != last_scope and o.scope.startswith("epic:"):
            print(f"--- epic {o.scope.removeprefix('epic:')} ---")
            last_scope = o.scope
        if o.ok:
            print(f"[VALIDATE-OK] {o.table} - clean")
        else:
            print(f"[VALIDATE-FAIL] {o.table} - {o.n_errors} errors", file=sys.stderr)
            for se in o.schema_errors:
                print(f"  - schema: {se}", file=sys.stderr)
            for ie in o.invariant_errors:
                print(f"  - {ie.render()}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Epic + single-file flows
# ---------------------------------------------------------------------------


def _validate_epic(
    *,
    epic_dir: Path,
    type_registry: TypeRegistry,
    allow_unknown_constraints: bool,
    output_format: str,
) -> tuple[list[ValidationOutcome], bool]:
    """Returns (outcomes, epic_failed). `epic_failed` is True when the epic
    structure itself is broken (missing contracts dir, etc.) — exit code 1."""
    contracts_dir = epic_dir / "contracts"
    if not contracts_dir.is_dir():
        _emit_init_error(f"{contracts_dir} not found", output_format)
        return [], True

    scope = f"epic:{epic_dir.name}"

    # Collect all table payloads first so cross-table FK validation can resolve.
    table_payloads: dict[str, dict] = {}
    table_paths: dict[str, Path] = {}
    joins_path: Path | None = None
    for yaml_path in sorted(contracts_dir.glob("*.yaml")):
        if yaml_path.name == "joins.yaml":
            joins_path = yaml_path
            continue
        payload = _load_yaml_safely(yaml_path, output_format)
        if payload is None:
            return [], True
        table = payload.get("table") or yaml_path.stem
        table_payloads[table] = payload
        table_paths[table] = yaml_path

    if not table_payloads:
        _emit_init_error(f"no contract YAMLs under {contracts_dir}", output_format)
        return [], True

    peer_field_names = {t: _field_names_from_payload(p) for t, p in table_payloads.items()}

    outcomes: list[ValidationOutcome] = []
    for table, payload in table_payloads.items():
        # peer set excludes the current table so self-references can't fool the FK check
        peer_subset = {t: names for t, names in peer_field_names.items() if t != table}
        outcomes.append(_validate_one_payload(
            payload=payload,
            path=table_paths[table],
            scope=scope,
            type_registry=type_registry,
            peer_field_names=peer_subset,
            allow_unknown_constraints=allow_unknown_constraints,
        ))

    if joins_path is not None:
        joins_payload = _load_yaml_safely(joins_path, output_format)
        if joins_payload is None:
            return outcomes, True
        outcomes.append(_validate_joins_payload(
            payload=joins_payload,
            path=joins_path,
            scope=scope,
            peer_field_names=peer_field_names,
        ))

    return outcomes, False


def _field_names_from_payload(payload: dict) -> set[str]:
    return {f.get("silver_name") for f in payload.get("fields", []) if f.get("silver_name") is not None}


def _load_yaml_safely(path: Path, output_format: str) -> dict | None:
    if not path.is_file():
        _emit_init_error(f"{path} not found", output_format)
        return None
    try:
        return load_yaml_mapping(path, what="contract YAML")
    except (yaml.YAMLError, ConfigError) as e:
        _emit_init_error(f"failed to parse {path}: {e}", output_format)
        return None


# ---------------------------------------------------------------------------
# Per-payload validation
# ---------------------------------------------------------------------------


def _validate_one_payload(
    *,
    payload: dict,
    path: Path,
    scope: str,
    type_registry: TypeRegistry,
    peer_field_names: dict[str, set[str]],
    allow_unknown_constraints: bool,
) -> ValidationOutcome:
    table = payload.get("table") or path.stem
    outcome = ValidationOutcome(scope=scope, table=table)

    outcome.schema_errors.extend(validate_against_schema(payload))
    if outcome.schema_errors:
        return outcome  # invariants run on a typed Contract; skip when shape is invalid.

    try:
        contract = Contract.from_dict(payload)
    except (KeyError, ValueError) as e:
        outcome.schema_errors.append(f"contract load failure: {e}")
        return outcome

    outcome.invariant_errors.extend(check_invariants(
        contract, type_registry, peer_field_names,
        allow_unknown_constraints=allow_unknown_constraints,
    ))
    outcome.invariant_errors.extend(check_path_invariants(payload, path))
    return outcome


def _validate_joins_payload(
    *,
    payload: dict,
    path: Path,
    scope: str,
    peer_field_names: dict[str, set[str]],
) -> ValidationOutcome:
    """Semantic validation for `joins.yaml`. No JSON Schema pass yet — the
    joins YAML has its own shape.
    """
    outcome = ValidationOutcome(scope=scope, table="joins")
    outcome.invariant_errors.extend(check_joins_invariants(payload, peer_field_names))
    outcome.invariant_errors.extend(check_path_invariants(payload, path))
    return outcome

