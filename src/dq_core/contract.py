"""Pure dataclasses for the contract surface: `Contract`, `FieldContract`,
`FieldCheck`, `Rejection`.

This module is intentionally framework-internal-import-free: it pulls in
shared core (`_util`, `errors`, `type_mapping`) and the FieldConstraint
registry, but NOT generation modules. So `from dq_core.contract
import Contract` from the validation side (or any extension) loads zero
generation code.

The build pipeline that produces a Contract from an Excel spec lives in
`data_contract.generation.builder` -- the only place that needs to know
how to turn a sheet row into a FieldContract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Union

from dq_core._util import load_yaml
from dq_core.yaml_io import FlowList
from dq_core.errors import RejectionError
from dq_core.field_constraints import constraint_for_contract_key
from dq_core.field_constraints.base import (
    FieldConstraint,
    unwrap_structured_value,
)
from dq_core.type_mapping import Type


CORE_FIELD_KEYS = frozenset({
    "name", "source_name", "type", "physical_type",
    "nullable", "description",
    "max_length", "precision", "scale",
    "primary_key", "foreign_key",
    "data_values",
})


@dataclass(frozen=True)
class FieldCheck:
    """A single dispatchable constraint occurrence on a field.

    Yielded by `FieldContract.iter_checks()` and `Contract.iter_field_checks()`.
    The downstream data validator iterates these and dispatches per
    `constraint_cls`; it does NOT need to know per-constraint wire formats.

    - `constraint_name`: REGISTRY key (e.g. "min_value", "default_value").
    - `contract_key`: how the constraint surfaces in the YAML (usually the same
      as constraint_name, sometimes different — e.g. `default_value` -> `default`).
    - `value`: per-field payload, already unwrapped from the structured shape.
    - `params`: contract_params slice; empty for flat constraints.
    """
    constraint_name: str
    contract_key: str
    constraint_cls: type[FieldConstraint]
    value: Any
    params: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldContract:
    name: str
    type: Type
    nullable: bool | None
    description: str | None
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    primary_key: bool | None = None
    foreign_key: dict[str, str] | None = None
    # Per-field acceptable raw tokens, keyed by canonical value as a string.
    # Today only BOOLEAN populates this: keys are "true" / "false", values
    # are the source-side tokens (case-insensitive, whitespace-stripped at
    # match time). The contract is the authoritative source: targets no
    # longer carry boolean data_values. Stamped onto each BOOLEAN field by
    # `build_contract` from the base type registry.
    data_values: dict[str, list[str]] | None = None
    # Raw business-friendly header from the spec ("Reference Number",
    # "Date d'envoi"). `name` is the slugified database identifier derived
    # from this. Omitted from `to_dict()` when it slugifies to `name` --
    # already-clean headers don't need both keys.
    source_name: str | None = None
    # Target-resolved physical type ("VARCHAR2(384 BYTE)", "BINARY_DOUBLE").
    # Derived at build time via `TypeRegistry.physical_type_for(field)`. The
    # validator always recomputes from the active target overlay; this is a
    # display field for spec readers, not an authoritative source.
    physical_type: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        # Omit source_name when it equals name -- avoids noise on already-
        # clean business headers that match their database slug.
        if self.source_name and self.source_name != self.name:
            out["source_name"] = self.source_name
        out["type"] = self.type.value
        if self.physical_type:
            out["physical_type"] = self.physical_type
        if self.nullable is not None:
            out["nullable"] = self.nullable
        if self.description:
            out["description"] = self.description
        if self.max_length is not None:
            out["max_length"] = self.max_length
        if self.precision is not None:
            out["precision"] = self.precision
        if self.scale is not None:
            out["scale"] = self.scale
        if self.primary_key is not None:
            out["primary_key"] = self.primary_key
        if self.foreign_key is not None:
            out["foreign_key"] = dict(self.foreign_key)
        if self.data_values is not None:
            # FlowList renders one line per token bucket: `'true': [1, oui, ...]`.
            out["data_values"] = {k: FlowList(v) for k, v in self.data_values.items()}
        for k, v in self.constraints.items():
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FieldContract":
        constraints = {k: v for k, v in payload.items() if k not in CORE_FIELD_KEYS}
        fk = payload.get("foreign_key")
        data_values_raw = payload.get("data_values")
        data_values: dict[str, list[str]] | None = None
        if isinstance(data_values_raw, dict):
            # Coerce keys to str so YAML's `true:` (bool) and `"true":` (str)
            # both load to the same field; values are kept user-readable
            # (the validator normalises at match time).
            data_values = {str(k).strip().lower(): list(v) for k, v in data_values_raw.items()}
        return cls(
            name=payload["name"],
            source_name=payload.get("source_name"),
            type=Type.from_canonical_string(payload["type"]),
            physical_type=payload.get("physical_type"),
            nullable=payload.get("nullable"),
            description=payload.get("description") or "",
            max_length=payload.get("max_length"),
            precision=payload.get("precision"),
            scale=payload.get("scale"),
            primary_key=payload.get("primary_key"),
            foreign_key=dict(fk) if isinstance(fk, dict) else None,
            data_values=data_values,
            constraints=constraints,
        )

    def iter_checks(self) -> Iterator[FieldCheck]:
        """Yield one FieldCheck per registered constraint declared on this field.

        Unregistered contract_keys (e.g. a constraint deregistered after the
        contract was written) are silently skipped — the validator's input
        contract may legitimately carry legacy state.
        """
        for contract_key in sorted(self.constraints):
            cls = constraint_for_contract_key(contract_key)
            if cls is None:
                continue
            raw = self.constraints[contract_key]
            value, params = unwrap_structured_value(raw)
            yield FieldCheck(
                constraint_name=cls.name,
                contract_key=cls.contract_key,
                constraint_cls=cls,
                value=value,
                params=params,
            )


@dataclass
class _Provenance:
    """Shared provenance fields for every generation artifact (per-table
    contracts/rejections AND the cross-table joins artifact).

    The joins artifact has no `table` field -- it spans tables -- so `table`
    lives on the `_TableProvenance` subclass below, not here. Anything that
    every artifact carries goes here.
    """
    version: str
    epic: str
    generated_at: str
    spec_file: str
    spec_sheet: str
    # Active target database. Stamped at generation from the epic version
    # config. Validation reads this to apply the matching target overlay
    # (physical types, bounds, length unit, parse formats) to the type
    # registry. Empty string means "no target" -- legacy contracts predating
    # this field, or hand-written test fixtures.
    target: str = ""

    def _provenance_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "epic": self.epic,
            "generated_at": self.generated_at,
            "spec": {"file_path": self.spec_file, "sheet_name": self.spec_sheet},
        }
        if self.target:
            out["target"] = self.target
        return out


@dataclass
class _TableProvenance(_Provenance):
    """Provenance for per-table artifacts (Contract / Rejection). Adds the
    `table` field and the `reject()` helper that mirrors provenance into a
    Rejection."""
    table: str = ""

    def _provenance_dict(self) -> dict[str, Any]:
        # Insert `table` between `spec` and the optional `target` so the
        # emitted YAML field order matches what pre-v9 contracts had.
        out: dict[str, Any] = {
            "version": self.version,
            "epic": self.epic,
            "generated_at": self.generated_at,
            "spec": {"file_path": self.spec_file, "sheet_name": self.spec_sheet},
            "table": self.table,
        }
        if self.target:
            out["target"] = self.target
        return out

    def reject(
        self,
        *,
        errors: list[RejectionError],
        spec_sheet: str | None = None,
        table: str | None = None,
    ) -> "Rejection":
        """Produce a `Rejection` that mirrors this Contract/Rejection's
        provenance, carrying the supplied errors.

        Optional `spec_sheet` / `table` overrides let cross-sheet duplicate
        detection emit a Rejection under a disambiguated table name without
        re-listing every other provenance field. Adding a new provenance
        field on `_TableProvenance` automatically propagates through this
        method.
        """
        return Rejection(
            version=self.version,
            epic=self.epic,
            generated_at=self.generated_at,
            spec_file=self.spec_file,
            spec_sheet=spec_sheet if spec_sheet is not None else self.spec_sheet,
            table=table if table is not None else self.table,
            target=self.target,
            errors=errors,
        )


@dataclass
class Contract(_TableProvenance):
    fields: list[FieldContract] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = self._provenance_dict()
        out["fields"] = [f.to_dict() for f in self.fields]
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Contract":
        spec = payload.get("spec") or {}
        return cls(
            version=str(payload["version"]),
            epic=str(payload["epic"]),
            generated_at=str(payload.get("generated_at", "")),
            spec_file=str(spec.get("file_path", "")),
            spec_sheet=str(spec.get("sheet_name", "")),
            table=str(payload["table"]),
            target=str(payload.get("target", "")),
            fields=[FieldContract.from_dict(f) for f in payload.get("fields", [])],
        )

    @classmethod
    def load(cls, path: Path) -> "Contract":
        return cls.from_dict(load_yaml(path))

    def iter_field_checks(self) -> Iterator[tuple["FieldContract", FieldCheck]]:
        """Walk every (field, check) pair across the contract.

        The downstream data validator iterates this once per contract and
        dispatches `check.constraint_cls` against the actual data per row.
        """
        for f in self.fields:
            for check in f.iter_checks():
                yield f, check

    def primary_key_fields(self) -> list["FieldContract"]:
        """Fields flagged as part of the primary key. Order matches `self.fields`."""
        return [f for f in self.fields if f.primary_key]

    def foreign_key_fields(self) -> list["FieldContract"]:
        """Fields carrying a foreign-key reference. Order matches `self.fields`."""
        return [f for f in self.fields if f.foreign_key is not None]

    def field_name_set(self) -> set[str]:
        """Quick lookup helper: just the set of field names on this contract.
        Used by cross-table FK / joins validation."""
        return {f.name for f in self.fields}


@dataclass
class Rejection(_TableProvenance):
    errors: list[RejectionError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = self._provenance_dict()
        out["errors"] = [e.to_dict() for e in self.errors]
        return out

    def prepend(self, errors: list[RejectionError]) -> None:
        """Splice `errors` to the front of this Rejection's error list.

        Side-effect only; mutates `self.errors` in place. Used to surface
        upstream causes (e.g. keys-sheet structural errors) ahead of the
        rejection's own errors.
        """
        self.errors = list(errors) + self.errors


BuildResult = Union[Contract, Rejection]
