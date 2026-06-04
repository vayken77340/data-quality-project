from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data_contract._util import now_iso_z
from data_contract.contract import Contract, FieldContract
from data_contract.field_constraints import constraint_for_contract_key
from data_contract.field_constraints.base import DriftChange


@dataclass
class DriftReport:
    table: str
    from_version: str
    to_version: str
    generated_at: str
    changes: list[DriftChange] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.changes

    def summary(self) -> dict[str, int]:
        out = {"breaking": 0, "additive": 0, "cosmetic": 0}
        for c in self.changes:
            if c.severity in out:
                out[c.severity] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "generated_at": self.generated_at,
            "summary": self.summary(),
            "changes": [c.to_dict() for c in self.changes],
        }


def diff_contracts(old: Contract, new: Contract, *, now: str | None = None) -> DriftReport:
    """Compute drift between two contracts (older → newer) over the same table."""
    changes: list[DriftChange] = []

    old_fields: dict[str, FieldContract] = {f.name: f for f in old.fields}
    new_fields: dict[str, FieldContract] = {f.name: f for f in new.fields}

    # Removed fields.
    for name in old_fields:
        if name not in new_fields:
            changes.append(DriftChange(
                kind="field_removed",
                severity="breaking",
                field=name,
                detail={"from_type": old_fields[name].type.value},
            ))

    # Added fields.
    for name, new_f in new_fields.items():
        if name in old_fields:
            continue
        # Required field with no escape valve is breaking; nullable additions are additive.
        is_breaking = new_f.nullable is False
        changes.append(DriftChange(
            kind="field_added",
            severity="breaking" if is_breaking else "additive",
            field=name,
            detail={"field_type": new_f.type.value, "nullable": bool(new_f.nullable)},
        ))

    # Mutated fields.
    for name, new_f in new_fields.items():
        old_f = old_fields.get(name)
        if old_f is None:
            continue
        changes.extend(_diff_field(name, old_f, new_f))

    return DriftReport(
        table=new.table,
        from_version=old.version,
        to_version=new.version,
        generated_at=now if now is not None else now_iso_z(),
        changes=changes,
    )


def _diff_field(name: str, old: FieldContract, new: FieldContract) -> list[DriftChange]:
    out: list[DriftChange] = []

    if old.type != new.type:
        out.append(DriftChange(
            kind="field_type_changed",
            severity="breaking",
            field=name,
            detail={"from": old.type.value, "to": new.type.value},
        ))

    if old.nullable != new.nullable:
        if old.nullable is True and new.nullable is False:
            out.append(DriftChange(
                kind="nullable_tightened",
                severity="breaking",
                field=name,
                detail={"from": True, "to": False},
            ))
        elif old.nullable is False and new.nullable is True:
            out.append(DriftChange(
                kind="nullable_relaxed",
                severity="additive",
                field=name,
                detail={"from": False, "to": True},
            ))
        else:
            out.append(DriftChange(
                kind="nullable_changed",
                severity="breaking",
                field=name,
                detail={"from": old.nullable, "to": new.nullable},
            ))

    if old.max_length != new.max_length:
        if old.max_length is None and new.max_length is not None:
            out.append(DriftChange(
                kind="max_length_tightened",
                severity="breaking",
                field=name,
                detail={"to": new.max_length},
            ))
        elif old.max_length is not None and new.max_length is None:
            out.append(DriftChange(
                kind="max_length_relaxed",
                severity="additive",
                field=name,
                detail={"from": old.max_length},
            ))
        elif new.max_length < old.max_length:
            out.append(DriftChange(
                kind="max_length_tightened",
                severity="breaking",
                field=name,
                detail={"from": old.max_length, "to": new.max_length},
            ))
        else:
            out.append(DriftChange(
                kind="max_length_relaxed",
                severity="additive",
                field=name,
                detail={"from": old.max_length, "to": new.max_length},
            ))

    if (old.description or "") != (new.description or ""):
        out.append(DriftChange(
            kind="description_changed",
            severity="cosmetic",
            field=name,
            detail={"from": old.description, "to": new.description},
        ))

    if bool(old.primary_key) != bool(new.primary_key):
        out.append(DriftChange(
            kind="primary_key_changed",
            severity="breaking",
            field=name,
            detail={"from": bool(old.primary_key), "to": bool(new.primary_key)},
        ))

    if (old.foreign_key or None) != (new.foreign_key or None):
        if old.foreign_key is None:
            out.append(DriftChange(
                kind="foreign_key_added",
                severity="breaking",
                field=name,
                detail={"to": dict(new.foreign_key)},
            ))
        elif new.foreign_key is None:
            out.append(DriftChange(
                kind="foreign_key_removed",
                severity="additive",
                field=name,
                detail={"from": dict(old.foreign_key)},
            ))
        else:
            out.append(DriftChange(
                kind="foreign_key_changed",
                severity="breaking",
                field=name,
                detail={"from": dict(old.foreign_key), "to": dict(new.foreign_key)},
            ))

    # Constraint diffs are dispatched through each registered constraint's `diff()` hook.
    constraint_keys = set(old.constraints) | set(new.constraints)
    for ck in sorted(constraint_keys):
        cls = constraint_for_contract_key(ck)
        if cls is None:
            # Unknown constraint key in the contract — surface as a generic change.
            if old.constraints.get(ck) != new.constraints.get(ck):
                out.append(DriftChange(
                    kind=f"{ck}_changed",
                    severity="breaking",
                    field=name,
                    detail={"from": old.constraints.get(ck), "to": new.constraints.get(ck)},
                ))
            continue
        change = cls.diff(name, old.constraints.get(ck), new.constraints.get(ck))
        if change is not None:
            out.append(change)

    return out
