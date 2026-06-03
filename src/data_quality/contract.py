from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Union

from data_quality._util import dump_yaml, load_yaml, now_iso_z
from data_quality.config import ColumnSpec, MergedConfig
from data_quality.errors import ErrorCollector, RejectionError
from data_quality.field_constraints.base import ConstraintContext
from data_quality.nullable import parse_nullable
from data_quality.spec_reader import RawField, SheetSpec
from data_quality.type_mapping import ParsedType, Type, TypeRegistry, parse_type, unknown_parsed_type


CORE_FIELD_KEYS = frozenset({
    "name", "type", "nullable", "description",
    "max_length", "precision", "scale",
    "primary_key", "foreign_key",
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
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
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type.value}
        if self.nullable is not None:
            out["nullable"] = self.nullable
        if self.description is not None:
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
        for k, v in self.constraints.items():
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FieldContract":
        constraints = {k: v for k, v in payload.items() if k not in CORE_FIELD_KEYS}
        fk = payload.get("foreign_key")
        return cls(
            name=payload["name"],
            type=Type(payload["type"]),
            nullable=payload.get("nullable"),
            description=payload.get("description"),
            max_length=payload.get("max_length"),
            precision=payload.get("precision"),
            scale=payload.get("scale"),
            primary_key=payload.get("primary_key"),
            foreign_key=dict(fk) if isinstance(fk, dict) else None,
            constraints=constraints,
        )


@dataclass
class _Provenance:
    """Shared fields + provenance-dict shape for Contract / Rejection."""
    version: str
    epic: str
    generated_at: str
    spec_file: str
    spec_sheet: str
    table: str

    def _provenance_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "epic": self.epic,
            "generated_at": self.generated_at,
            "source": {"spec_file": self.spec_file, "spec_sheet": self.spec_sheet},
            "table": self.table,
        }


@dataclass
class Contract(_Provenance):
    fields: list[FieldContract] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = self._provenance_dict()
        out["fields"] = [f.to_dict() for f in self.fields]
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Contract":
        src = payload.get("source") or {}
        return cls(
            version=str(payload["version"]),
            epic=str(payload["epic"]),
            generated_at=str(payload.get("generated_at", "")),
            spec_file=str(src.get("spec_file", "")),
            spec_sheet=str(src.get("spec_sheet", "")),
            table=str(payload["table"]),
            fields=[FieldContract.from_dict(f) for f in payload.get("fields", [])],
        )

    @classmethod
    def load(cls, path: Path) -> "Contract":
        return cls.from_dict(load_yaml(path))


@dataclass
class Rejection(_Provenance):
    errors: list[RejectionError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = self._provenance_dict()
        out["errors"] = [e.to_dict() for e in self.errors]
        return out


BuildResult = Union[Contract, Rejection]


# ---------------------------------------------------------------------------
# build_contract
# ---------------------------------------------------------------------------


def _check_mandatory_blank(
    raw: object | None,
    col: ColumnSpec,
    *,
    field_name: str,
    sheet_row: int,
) -> tuple[str | None, RejectionError | None]:
    """Shared 'is this cell blank, and is that ok?' check.

    Returns (trimmed string, None) if a value is present.
    Returns (None, missing_mandatory error) if blank and `col.value_required`.
    Returns (None, None) if blank and optional.
    """
    if raw is None or str(raw).strip() == "":
        if col.value_required:
            return None, RejectionError(
                kind="missing_mandatory",
                sheet_row=sheet_row,
                column=col.spec_name,
                field=field_name,
                message=f"field {field_name!r} (column {col.spec_name!r}) requires a value but the cell is empty",
            )
        return None, None
    return str(raw).strip(), None


def build_contract(
    merged: MergedConfig,
    sheet_spec: SheetSpec,
    rows: Iterable[RawField],
    *,
    type_registry: TypeRegistry,
    spec_file_rel: str,
    table_name_from_config: str,
    allow_unknown_types: bool = False,
    now: str | None = None,
) -> BuildResult:
    """Build a Contract for the given table, or a Rejection if the spec has errors.

    Rules:
    - Errors are collected, not raised. Only the final return type changes.
    - The output `table:` is the Table-column value (if present and consistent on every row),
      else `sheet_spec.sheet_name`. The epic config's `table_name` is used only to pick the sheet.
    """
    collector = ErrorCollector()
    fields: list[FieldContract] = []
    seen_field_names: dict[str, int] = {}
    cm = merged.column_mapping
    table_values: list[str] = []

    for row in rows:
        # name / type / description: shared blank-check + mandatory handling.
        name_value, err = _check_mandatory_blank(row.name_raw, cm.name, field_name="name", sheet_row=row.sheet_row)
        if err: collector.add(err)

        description_value, err = _check_mandatory_blank(row.description_raw, cm.description, field_name="description", sheet_row=row.sheet_row)
        if err: collector.add(err)

        # Type goes through the registry rather than the blank-check helper because
        # it produces a structured ParsedType (or downgrades to UNKNOWN).
        parsed_type: ParsedType | None
        type_str, type_blank_err = _check_mandatory_blank(row.type_raw, cm.type, field_name="type", sheet_row=row.sheet_row)
        if type_blank_err:
            collector.add(type_blank_err)
            parsed_type = None
        elif type_str is None:
            parsed_type = None
        else:
            parsed_type, type_err = parse_type(type_str, type_registry, sheet_row=row.sheet_row)
            if type_err is not None:
                if allow_unknown_types:
                    parsed_type = unknown_parsed_type()
                else:
                    collector.add(RejectionError(
                        kind=type_err.kind,
                        sheet_row=type_err.sheet_row,
                        column=cm.type.spec_name,
                        field="type",
                        value=type_err.value,
                        message=type_err.message,
                    ))

        # Nullable: own dedicated parser (has its own missing/invalid kinds).
        nullable_value, null_err = parse_nullable(row.nullable_raw, cm.nullable, sheet_row=row.sheet_row)
        if null_err: collector.add(null_err)

        # Table column: validated only, not put on the field. Use the shared
        # mandatory-blank check for symmetry, then accumulate non-empty values.
        if sheet_spec.has_table_column and cm.table is not None:
            tval, err = _check_mandatory_blank(row.table_raw, cm.table, field_name="table", sheet_row=row.sheet_row)
            if err: collector.add(err)
            if tval is not None:
                table_values.append(tval)

        # Constraints — parsed once the field's resolved type/max_length is known.
        constraint_values: dict[str, Any] = {}
        if parsed_type is not None and cm.constraints:
            ctx = ConstraintContext(
                sheet_row=row.sheet_row,
                field_type=parsed_type.type,
                field_max_length=parsed_type.max_length,
            )
            for c_name, constraint in cm.constraints.items():
                value, err = constraint.parse_cell(row.extras.get(c_name), ctx)
                if err is not None:
                    collector.add(err)
                    continue
                if value is not None:
                    constraint_values[constraint.contract_key] = value

        if name_value is not None and parsed_type is not None:
            prev_row = seen_field_names.get(name_value)
            if prev_row is not None:
                collector.add(RejectionError(
                    kind="duplicate_field",
                    sheet_row=row.sheet_row,
                    column=cm.name.spec_name,
                    field="name",
                    value=name_value,
                    message=f"duplicate field name {name_value!r} (first seen at sheet row {prev_row})",
                ))
            else:
                seen_field_names[name_value] = row.sheet_row
                fields.append(FieldContract(
                    name=name_value,
                    type=parsed_type.type,
                    nullable=nullable_value,
                    description=description_value,
                    max_length=parsed_type.max_length,
                    precision=parsed_type.precision,
                    scale=parsed_type.scale,
                    constraints=constraint_values,
                ))

    # Resolve the contract's table name.
    table_name = sheet_spec.sheet_name
    if table_values:
        distinct = sorted(set(table_values))
        if len(distinct) > 1:
            collector.add(RejectionError(
                kind="multi_table_in_sheet",
                column=cm.table.spec_name if cm.table else None,
                field="table",
                value=distinct,
                message=(
                    f"sheet {sheet_spec.sheet_name!r} contains rows for multiple tables: {distinct}; "
                    "multi-table sheets are not yet supported"
                ),
            ))
        else:
            table_name = distinct[0]

    generated_at = now if now is not None else now_iso_z()
    common = dict(
        version=merged.version,
        epic=merged.epic,
        generated_at=generated_at,
        spec_file=spec_file_rel,
        spec_sheet=sheet_spec.sheet_name,
        table=table_name,
    )

    if collector.has_errors():
        return Rejection(**common, errors=collector.errors)
    return Contract(**common, fields=fields)


# ---------------------------------------------------------------------------
# Output paths + writers
# ---------------------------------------------------------------------------


def write_outputs(result: BuildResult, contracts_dir: Path) -> list[Path]:
    """Materialize a Contract or Rejection, cleaning up the opposite side."""
    canonical = contracts_dir / f"{result.table}.yaml"
    rejected = contracts_dir / "rejected" / f"{result.table}.yaml"
    history = history_path_for(result, contracts_dir)

    if isinstance(result, Contract):
        dump_yaml(canonical, result.to_dict())
        dump_yaml(history, result.to_dict())
        if rejected.exists():
            rejected.unlink()
        return [canonical, history]

    dump_yaml(rejected, result.to_dict())
    if canonical.exists():
        canonical.unlink()
    return [rejected]


def history_path_for(contract_or_rejection: BuildResult, contracts_dir: Path) -> Path:
    return contracts_dir / "history" / contract_or_rejection.table / f"v{contract_or_rejection.version}.yaml"


def drift_path_for(contracts_dir: Path, table: str, from_version: str, to_version: str) -> Path:
    return contracts_dir / "drift" / f"{table}__v{from_version}_to_v{to_version}.yaml"


def write_history_only(contract: Contract, contracts_dir: Path) -> Path:
    """Backfill helper: write only the versioned history snapshot, leaving canonical/rejected untouched."""
    history = history_path_for(contract, contracts_dir)
    dump_yaml(history, contract.to_dict())
    return history
