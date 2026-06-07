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

from data_contract.contract import Contract
from data_contract.errors import ConfigError
from data_contract.field_constraints import constraint_for_contract_key
from data_contract.field_constraints.base import unwrap_structured_value
from data_contract.joins import JOIN_TYPE_ALIASES
from data_contract.schema_export import validate_against_schema
from data_contract.type_mapping import Type, TypeRegistry, load_type_registry


VALID_JOIN_TYPES = frozenset(JOIN_TYPE_ALIASES.values())
VALID_CARDINALITIES = frozenset({"1:1", "1:n", "n:1", "n:m"})


@dataclass(frozen=True)
class InvariantError:
    kind: str
    table: str
    field: str | None
    message: str

    def render(self) -> str:
        field_part = f" field {self.field!r}" if self.field else ""
        return f"{self.kind}:{field_part} {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "table": self.table,
            "field": self.field,
            "message": self.message,
        }


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
    if file is not None and epic is not None:
        _emit_init_error("--file and --epic are mutually exclusive", output_format)
        return 1

    try:
        type_registry = load_type_registry(types_path)
    except ConfigError as e:
        _emit_init_error(f"config error: {e}", output_format)
        return 1

    outcomes: list[ValidationOutcome] = []
    epic_failed = False

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
        epics = [epic] if epic is not None else _discover_epics(epic_root)
        if not epics:
            _emit_init_error(f"no epics found under {epic_root}", output_format)
            return 1
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
            stats = _stats_for_table_payload(o)
            print(f"[VALIDATE-OK] {o.table} - {stats}")
        else:
            print(f"[VALIDATE-FAIL] {o.table} - {o.n_errors} errors", file=sys.stderr)
            for se in o.schema_errors:
                print(f"  - schema: {se}", file=sys.stderr)
            for ie in o.invariant_errors:
                print(f"  - {ie.render()}", file=sys.stderr)


def _stats_for_table_payload(outcome: ValidationOutcome) -> str:
    # The outcome doesn't carry the contract object, but the table line is
    # already informative enough; keep the OK summary concise.
    return f"clean"


# ---------------------------------------------------------------------------
# Epic + single-file flows
# ---------------------------------------------------------------------------


def _discover_epics(epic_root: Path) -> list[str]:
    if not epic_root.is_dir():
        return []
    return sorted(
        c.name for c in epic_root.iterdir()
        if c.is_dir() and (c / "contracts").is_dir()
    )


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
    return {f.get("name") for f in payload.get("fields", []) if f.get("name") is not None}


def _load_yaml_safely(path: Path, output_format: str) -> dict | None:
    if not path.is_file():
        _emit_init_error(f"{path} not found", output_format)
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
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


# ---------------------------------------------------------------------------
# Invariant engines
# ---------------------------------------------------------------------------


def check_invariants(
    contract: Contract,
    type_registry: TypeRegistry,
    peer_field_names: dict[str, set[str]],
    *,
    allow_unknown_constraints: bool,
) -> list[InvariantError]:
    """Semantic-layer checks on a Contract. Schema-validated input assumed.

    `peer_field_names` is a `table_name -> set of field names` map used to
    resolve foreign-key targets. Generator builds this from its in-flight
    contracts; validate-contract builds it from loaded YAMLs.
    """
    out: list[InvariantError] = []
    seen_names: dict[str, bool] = {}

    for f in contract.fields:
        if f.name in seen_names:
            out.append(InvariantError(
                kind="duplicate_field_names",
                table=contract.table, field=f.name,
                message=f"field name {f.name!r} appears more than once on table {contract.table!r}",
            ))
        seen_names[f.name] = True

        if f.primary_key and f.nullable is True:
            out.append(InvariantError(
                kind="pk_must_not_be_nullable",
                table=contract.table, field=f.name,
                message="primary key field has nullable=true",
            ))

        if f.max_length is not None and f.type is not Type.VARCHAR:
            out.append(InvariantError(
                kind="max_length_only_on_varchar",
                table=contract.table, field=f.name,
                message=f"max_length is set on a {f.type.value!r} field (only varchar carries max_length)",
            ))

        # precision/scale apply to numeric types (double, float).
        for attr in ("precision", "scale"):
            if getattr(f, attr) is not None and f.type not in (Type.DOUBLE, Type.FLOAT):
                out.append(InvariantError(
                    kind="precision_scale_only_on_numerics",
                    table=contract.table, field=f.name,
                    message=f"{attr} is set on a {f.type.value!r} field (only double/float carry precision/scale)",
                ))

        if f.precision is not None and f.scale is not None and f.scale > f.precision:
            out.append(InvariantError(
                kind="precision_scale_consistency",
                table=contract.table, field=f.name,
                message=f"scale ({f.scale}) is greater than precision ({f.precision})",
            ))

        for ck, raw_value in f.constraints.items():
            cls = constraint_for_contract_key(ck)
            if cls is None:
                if not allow_unknown_constraints:
                    out.append(InvariantError(
                        kind="constraint_keys_registered",
                        table=contract.table, field=f.name,
                        message=f"contract key {ck!r} is not in the constraint registry",
                    ))
                continue
            if cls.CONTRACT_FIELDS and not isinstance(raw_value, dict):
                out.append(InvariantError(
                    kind="constraint_structured_shape",
                    table=contract.table, field=f.name,
                    message=f"{ck} expects a structured value (dict with {list(cls.CONTRACT_FIELDS)}); got {type(raw_value).__name__}",
                ))

        min_raw = f.constraints.get("min_value")
        max_raw = f.constraints.get("max_value")
        if min_raw is not None and max_raw is not None:
            min_v, _ = unwrap_structured_value(min_raw)
            max_v, _ = unwrap_structured_value(max_raw)
            try:
                if min_v > max_v:
                    out.append(InvariantError(
                        kind="min_value_max_value_consistency",
                        table=contract.table, field=f.name,
                        message=f"min_value ({min_v}) is greater than max_value ({max_v})",
                    ))
            except TypeError:
                pass

        if f.foreign_key is not None and peer_field_names:
            tgt_table = f.foreign_key.get("table")
            tgt_col = f.foreign_key.get("column")
            target_fields = peer_field_names.get(tgt_table)
            if target_fields is None:
                out.append(InvariantError(
                    kind="fk_target_exists",
                    table=contract.table, field=f.name,
                    message=f"foreign key references unknown table {tgt_table!r}",
                ))
            elif tgt_col not in target_fields:
                out.append(InvariantError(
                    kind="fk_target_exists",
                    table=contract.table, field=f.name,
                    message=f"foreign key references {tgt_table}.{tgt_col} but that column does not exist",
                ))

    return out


def check_path_invariants(payload: dict, path: Path) -> list[InvariantError]:
    """Check the YAML's `epic` / `version` against the path on disk.

    Recognized layouts:
      epics/<E>/contracts/<table>.yaml             -> epic == E
      epics/<E>/contracts/joins.yaml               -> epic == E
      epics/<E>/contracts/history/<v>/<table>.yaml -> epic == E AND version == v

    A path that doesn't match any of these is silently skipped — `--file`
    against an arbitrary location shouldn't trigger spurious errors.
    """
    out: list[InvariantError] = []
    try:
        parts = path.resolve().parts
    except OSError:
        return out

    if "epics" not in parts:
        return out
    epics_idx = parts.index("epics")
    if epics_idx + 1 >= len(parts):
        return out

    expected_epic = parts[epics_idx + 1]
    actual_epic = str(payload.get("epic", ""))
    table = str(payload.get("table") or path.stem)
    if actual_epic and actual_epic != expected_epic:
        out.append(InvariantError(
            kind="epic_matches_path",
            table=table, field=None,
            message=(
                f"YAML declares epic {actual_epic!r} but lives under "
                f"epics/{expected_epic}/"
            ),
        ))

    if "history" in parts[epics_idx:]:
        hist_idx = parts.index("history", epics_idx)
        if hist_idx + 1 < len(parts) - 1:  # there's at least one path segment after history/
            expected_version = parts[hist_idx + 1]
            actual_version = str(payload.get("version", ""))
            if actual_version and actual_version != expected_version:
                out.append(InvariantError(
                    kind="version_matches_path",
                    table=table, field=None,
                    message=(
                        f"YAML declares version {actual_version!r} but lives under "
                        f"history/{expected_version}/"
                    ),
                ))
    return out


def check_joins_invariants(
    payload: dict,
    peer_field_names: dict[str, set[str]],
) -> list[InvariantError]:
    """Validate a `joins.yaml` against the peer contracts in the same epic."""
    out: list[InvariantError] = []
    joins = payload.get("joins") or []
    if not isinstance(joins, list):
        out.append(InvariantError(
            kind="joins_shape", table="joins", field=None,
            message="joins payload's `joins:` key must be a list",
        ))
        return out

    for i, entry in enumerate(joins):
        if not isinstance(entry, dict):
            out.append(InvariantError(
                kind="joins_shape", table="joins", field=f"joins[{i}]",
                message="join entry must be a mapping",
            ))
            continue

        st = entry.get("source_table")
        sc = entry.get("source_column")
        tt = entry.get("target_table")
        tc = entry.get("target_column")
        jt = entry.get("type")

        for required, label in ((st, "source_table"), (sc, "source_column"),
                                (tt, "target_table"), (tc, "target_column"),
                                (jt, "type")):
            if not required:
                out.append(InvariantError(
                    kind="joins_missing_required", table="joins", field=f"joins[{i}].{label}",
                    message=f"required field {label!r} is missing or empty",
                ))

        if jt and jt not in VALID_JOIN_TYPES:
            out.append(InvariantError(
                kind="joins_unknown_type", table="joins", field=f"joins[{i}].type",
                message=f"join type {jt!r} is not one of {sorted(VALID_JOIN_TYPES)}",
            ))

        card = entry.get("cardinality")
        if card is not None and card not in VALID_CARDINALITIES:
            out.append(InvariantError(
                kind="joins_unknown_cardinality", table="joins", field=f"joins[{i}].cardinality",
                message=f"cardinality {card!r} is not one of {sorted(VALID_CARDINALITIES)}",
            ))

        if st and st not in peer_field_names:
            out.append(InvariantError(
                kind="joins_unknown_table", table="joins", field=f"joins[{i}].source_table",
                message=f"source_table {st!r} is not a generated contract in this epic",
            ))
        elif st and sc and sc not in peer_field_names[st]:
            out.append(InvariantError(
                kind="joins_unknown_column", table="joins", field=f"joins[{i}].source_column",
                message=f"source_column {sc!r} is not a field of {st!r}",
            ))

        if tt and tt not in peer_field_names:
            out.append(InvariantError(
                kind="joins_unknown_table", table="joins", field=f"joins[{i}].target_table",
                message=f"target_table {tt!r} is not a generated contract in this epic",
            ))
        elif tt and tc and tc not in peer_field_names[tt]:
            out.append(InvariantError(
                kind="joins_unknown_column", table="joins", field=f"joins[{i}].target_column",
                message=f"target_column {tc!r} is not a field of {tt!r}",
            ))

    return out
