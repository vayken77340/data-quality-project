"""Spec to contract build pipeline + on-disk output paths.

Takes a SheetSpec (sheet name + iter of raw field rows from the spec
workbook) and a MergedConfig (the per-epic column_mapping) and produces a
`Contract` (or a `Rejection` carrying every collected error). Also owns
the output-path conventions and the writer that materialises canonical /
history / rejected YAMLs.

This module is the ONLY generation-side place that touches `Contract`'s
constructor directly with build-time inputs. Everything else (validation,
schema export, drift, etc.) reads contracts via `Contract.from_dict` /
`Contract.load` -- the build pipeline is the producer; the rest are
consumers.

Split off from `data_contract/contract.py` so the contract dataclass
module has zero generation imports. See the docstring at the top of
`contract.py`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Union

from data_contract._util import now_iso_z
from data_contract.contract import (
    BuildResult,
    Contract,
    FieldContract,
    Rejection,
)
from data_contract.core.column_ref import ColumnRef
from data_contract.core.slugify import slugify
from data_contract.core.yaml_io import dump_yaml
from data_contract.errors import ErrorCollector, RejectionError
from data_contract.field_constraints.base import ConstraintContext
from data_contract.generation.config import MergedConfig
from data_contract.generation.nullable import parse_nullable
from data_contract.generation.spec_reader import RawField, SheetSpec
from data_contract.type_mapping import (
    ParsedType,
    Type,
    TypeRegistry,
    parse_type,
    unknown_parsed_type,
)


def _check_mandatory_blank(
    raw: object | None,
    col: ColumnRef,
    *,
    field_name: str,
    sheet_row: int,
) -> tuple[Any, RejectionError | None]:
    """Adapter that exposes `ColumnRef.read_cell` under the historical name.

    Kept as a thin wrapper so call sites stay terse; behaviour lives on
    `ColumnRef` for symmetry with the constraint-side blank-handling
    template (`FieldConstraint.parse_cell`).
    """
    return col.read_cell(raw, sheet_row=sheet_row, field_name=field_name)


# ---------------------------------------------------------------------------
# build_contract
# ---------------------------------------------------------------------------


def _build_one_field(
    row: RawField,
    cm,
    sheet_spec: SheetSpec,
    type_registry: TypeRegistry,
    *,
    allow_unknown_types: bool,
) -> tuple[FieldContract | None, list[RejectionError], str | None]:
    """Process one spec row into at most one `FieldContract`.

    Returns `(field, errors, table_value)`:
      - `field` is None when name or type couldn't be resolved.
      - `errors` carries every blank/missing/parse failure encountered.
      - `table_value` is the raw Table-column value for this row when the sheet
        has a Table column (used by the caller to detect multi-table sheets);
        None otherwise.

    Duplicate-name detection lives in the caller -- it is cross-row state.
    """
    errors: list[RejectionError] = []

    source_name_value, err = _check_mandatory_blank(
        row.name_raw, cm.name, field_name="name", sheet_row=row.sheet_row,
    )
    if err: errors.append(err)

    # Optional `Nom BDD` override: when present, used verbatim as the field's
    # `name` (skipping slugify). When absent, `name` is `slugify(source_name)`.
    db_name_override: str | None = None
    if cm.db_name is not None and row.db_name_raw is not None:
        db_raw = str(row.db_name_raw).strip()
        if db_raw:
            db_name_override = db_raw

    if source_name_value:
        name_value: str | None = db_name_override or slugify(source_name_value)
    else:
        name_value = None

    description_value, err = _check_mandatory_blank(
        row.description_raw, cm.description, field_name="description", sheet_row=row.sheet_row,
    )
    if err: errors.append(err)
    # Normalise empty descriptions to None so `FieldContract.to_dict` omits
    # the key. specs_parsing.yaml ships description with `default_value: ""`,
    # which would otherwise stamp every undocumented field with `description: ''`.
    if isinstance(description_value, str) and description_value.strip() == "":
        description_value = None

    # Type goes through the registry rather than the blank-check helper because
    # it produces a structured ParsedType (or downgrades to UNKNOWN).
    parsed_type: ParsedType | None
    type_str, type_blank_err = _check_mandatory_blank(
        row.type_raw, cm.type, field_name="type", sheet_row=row.sheet_row,
    )
    if type_blank_err:
        errors.append(type_blank_err)
        parsed_type = None
    elif type_str is None:
        parsed_type = None
    else:
        parsed_type, type_err = parse_type(type_str, type_registry, sheet_row=row.sheet_row)
        if type_err is not None:
            if allow_unknown_types:
                parsed_type = unknown_parsed_type()
            else:
                errors.append(RejectionError(
                    kind=type_err.kind,
                    sheet_row=type_err.sheet_row,
                    column=cm.type.spec_name,
                    field="type",
                    value=type_err.value,
                    message=type_err.message,
                ))

    nullable_value, null_err = parse_nullable(row.nullable_raw, cm.nullable, sheet_row=row.sheet_row)
    if null_err: errors.append(null_err)

    table_value: str | None = None
    if sheet_spec.has_table_column and cm.table is not None:
        tval, err = _check_mandatory_blank(
            row.table_raw, cm.table, field_name="table", sheet_row=row.sheet_row,
        )
        if err: errors.append(err)
        if tval is not None:
            table_value = tval

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
                errors.append(err)
                continue
            if value is not None:
                constraint_values[constraint.contract_key] = constraint.to_contract_value(value)

    if name_value is None or parsed_type is None:
        return None, errors, table_value

    # For BOOLEAN, stamp the universal data_values block (from the base type
    # registry) onto the field. The contract carries its own authoritative
    # token list; targets no longer dictate which tokens are valid.
    field_data_values = None
    if parsed_type.type is Type.BOOLEAN:
        base_tokens = type_registry.data_values_for(Type.BOOLEAN)
        if base_tokens is not None:
            field_data_values = {
                literal: sorted(tokens) for literal, tokens in base_tokens.items()
            }

    # Emit `source_name` only when it differs from the database `name`. Holds
    # the verbatim spec value ("Reference Number") so the validator can match
    # raw CSV/Excel/JSON headers without needing a separate field_mapping block.
    field_source_name: str | None = None
    if source_name_value and source_name_value != name_value:
        field_source_name = source_name_value

    field = FieldContract(
        name=name_value,
        source_name=field_source_name,
        type=parsed_type.type,
        nullable=nullable_value,
        description=description_value,
        max_length=parsed_type.max_length,
        precision=parsed_type.precision,
        scale=parsed_type.scale,
        data_values=field_data_values,
        constraints=constraint_values,
    )
    # Stamp the target-resolved physical type. Derived from the active target
    # overlay (`TypeRegistry.physical_type_for`) and re-derived on validation,
    # so this is a display field only -- the validator never trusts it.
    physical = type_registry.physical_type_for(field)
    if physical is not None:
        field = replace(field, physical_type=physical)
    return field, errors, table_value


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
        field, row_errors, table_value = _build_one_field(
            row, cm, sheet_spec, type_registry,
            allow_unknown_types=allow_unknown_types,
        )
        for err in row_errors:
            collector.add(err)
        if table_value is not None:
            table_values.append(table_value)
        if field is None:
            continue
        prev_row = seen_field_names.get(field.name)
        if prev_row is not None:
            collector.add(RejectionError(
                kind="duplicate_field",
                sheet_row=row.sheet_row,
                column=cm.name.spec_name,
                field="name",
                value=field.name,
                message=f"duplicate field name {field.name!r} (first seen at sheet row {prev_row})",
            ))
            continue
        seen_field_names[field.name] = row.sheet_row
        fields.append(field)

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
        target=merged.target,
    )

    if collector.has_errors():
        return Rejection(**common, errors=collector.errors)
    return Contract(**common, fields=fields)


# ---------------------------------------------------------------------------
# Output paths + writers
# ---------------------------------------------------------------------------


def write_outputs(
    result: BuildResult,
    contracts_dir: Path,
    *,
    write_history: bool = True,
) -> list[Path]:
    """Materialize a Contract or Rejection, cleaning up the opposite side.

    `write_history=False` skips the versioned snapshot, leaving the canonical
    contract as the sole on-disk output for a successful build.
    """
    canonical = contracts_dir / f"{result.table}.yaml"
    rejected = contracts_dir / "rejected" / f"{result.table}.yaml"

    if isinstance(result, Contract):
        dump_yaml(canonical, result.to_dict())
        paths = [canonical]
        if write_history:
            history = history_path_for(result, contracts_dir)
            dump_yaml(history, result.to_dict())
            paths.append(history)
        if rejected.exists():
            rejected.unlink()
        return paths

    dump_yaml(rejected, result.to_dict())
    if canonical.exists():
        canonical.unlink()
    return [rejected]


def history_path_for(contract_or_rejection: BuildResult, contracts_dir: Path) -> Path:
    return contracts_dir / "history" / contract_or_rejection.version / f"{contract_or_rejection.table}.yaml"


def history_path_for_table(contracts_dir: Path, version: str, table: str) -> Path:
    """Path-only variant for callers that don't have a BuildResult handy (e.g. backfill existence checks)."""
    return contracts_dir / "history" / version / f"{table}.yaml"


def drift_path_for(contracts_dir: Path, table: str, from_version: str, to_version: str) -> Path:
    return contracts_dir / "drift" / f"{table}__v{from_version}_to_v{to_version}.yaml"


def write_history_only(contract: Contract, contracts_dir: Path) -> Path:
    """Backfill helper: write only the versioned history snapshot, leaving canonical/rejected untouched."""
    history = history_path_for(contract, contracts_dir)
    dump_yaml(history, contract.to_dict())
    return history
