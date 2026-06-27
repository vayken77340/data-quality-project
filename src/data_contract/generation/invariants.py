"""Contract invariant engine.

Pure functions that walk a `Contract` (or a `joins.yaml` payload) and
collect semantic-layer errors the JSON Schema can't express: PK not
nullable, max_length only on strings, FK targets exist, etc.

Two callers:

* `generation.validate_contract.run_validate_contract` — runs the engine
  on on-disk YAMLs after the JSON Schema pass.
* `generation.pipeline.self_check_post_build` — runs the engine on the
  in-memory contracts a build just produced, so emission bugs surface
  at the source instead of one round-trip later.

No I/O lives here. No CLI plumbing. No output formatting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_contract.contract import Contract
from data_contract.field_constraints import constraint_for_contract_key
from data_contract.field_constraints.base import unwrap_structured_value
from data_contract.generation.joins import JOIN_TYPE_ALIASES, JoinsContract
from data_contract.type_mapping import Type, TypeRegistry


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


@dataclass(frozen=True)
class InMemoryInvariantsResult:
    """Output of `check_invariants_in_memory`. `per_table[T]` is the (possibly
    empty) list of errors for table T; `joins` is the (possibly empty) list of
    join-level errors."""
    per_table: dict[str, list["InvariantError"]]
    joins: list["InvariantError"]


def check_invariants_in_memory(
    contracts_by_table: dict[str, Contract],
    joins_contract: JoinsContract | None,
    type_registry: TypeRegistry,
    *,
    allow_unknown_constraints: bool = False,
) -> InMemoryInvariantsResult:
    """Run the same invariant engine `validate-contract` uses on the on-disk
    YAMLs, but against in-memory `Contract` objects (and an optional in-memory
    `JoinsContract`). Used by `pipeline.self_check_post_build` after a build
    so emission bugs surface at the source instead of one round-trip later.
    """
    peer_field_names = {t: c.field_name_set() for t, c in contracts_by_table.items()}
    per_table: dict[str, list[InvariantError]] = {}
    for table, contract in contracts_by_table.items():
        peer_subset = {t: n for t, n in peer_field_names.items() if t != table}
        per_table[table] = check_invariants(
            contract, type_registry, peer_subset,
            allow_unknown_constraints=allow_unknown_constraints,
        )
    joins_errors: list[InvariantError] = []
    if joins_contract is not None:
        joins_errors = check_joins_invariants(joins_contract.to_dict(), peer_field_names)
    return InMemoryInvariantsResult(per_table=per_table, joins=joins_errors)


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

        if f.max_length is not None and f.type is not Type.STRING:
            out.append(InvariantError(
                kind="max_length_only_on_string",
                table=contract.table, field=f.name,
                message=f"max_length is set on a {f.type.value!r} field (only string carries max_length)",
            ))

        # precision/scale apply to decimal only. IEEE float types do NOT carry
        # application-level precision -- that's what `decimal(p, s)` is for.
        for attr in ("precision", "scale"):
            if getattr(f, attr) is not None and f.type is not Type.DECIMAL:
                out.append(InvariantError(
                    kind="precision_scale_only_on_decimal",
                    table=contract.table, field=f.name,
                    message=f"{attr} is set on a {f.type.value!r} field (only decimal carries precision/scale)",
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
