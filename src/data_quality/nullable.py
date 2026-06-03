from __future__ import annotations

from dataclasses import dataclass, field

from data_quality.errors import ConfigError, RejectionError


@dataclass(frozen=True)
class NullableMapping:
    spec_name: str
    true_values: frozenset[str]
    false_values: frozenset[str]
    required: bool = True
    value_required: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "NullableMapping":
        spec_name = raw.get("spec_name")
        if not isinstance(spec_name, str) or not spec_name:
            raise ConfigError("column_mapping.nullable.spec_name must be a non-empty string")
        required = bool(raw.get("required", True))
        value_required = bool(raw.get("value_required", False))
        if not required and value_required:
            raise ConfigError(
                "column_mapping.nullable: cannot have `required: false` with `value_required: true`. "
                "A column whose existence is optional cannot also require values per row."
            )
        values = raw.get("values") or {}
        if not isinstance(values, dict):
            raise ConfigError("column_mapping.nullable.values must be a mapping of 'true'/'false' -> list")
        true_list = values.get("true") or values.get(True) or []
        false_list = values.get("false") or values.get(False) or []
        if not isinstance(true_list, list) or not isinstance(false_list, list):
            raise ConfigError("column_mapping.nullable.values 'true'/'false' entries must be lists")
        true_set = frozenset(_normalize_token(v) for v in true_list)
        false_set = frozenset(_normalize_token(v) for v in false_list)
        overlap = true_set & false_set
        if overlap:
            raise ConfigError(
                f"column_mapping.nullable.values: tokens appear under both true and false: {sorted(overlap)}"
            )
        return cls(
            spec_name=spec_name,
            required=required,
            value_required=value_required,
            true_values=true_set,
            false_values=false_set,
        )


def _normalize_token(v: object) -> str:
    return str(v).strip().lower()


def parse_nullable(
    raw: object,
    mapping: NullableMapping,
    *,
    sheet_row: int,
) -> tuple[bool | None, RejectionError | None]:
    """Parse a single Obligatoire-style cell into a nullable bool.

    Returns:
      (True/False, None)   on a recognized value
      (None, RejectionError(missing_mandatory)) on empty + value_required
      (None, None) on empty + not value_required (caller omits nullable from the contract)
      (None, RejectionError(invalid_nullable)) on an unrecognized token
    """
    is_empty = raw is None or str(raw).strip() == ""
    if is_empty:
        if mapping.value_required:
            return None, RejectionError(
                kind="missing_mandatory",
                sheet_row=sheet_row,
                column=mapping.spec_name,
                field="nullable",
                message=f"field 'nullable' (column {mapping.spec_name!r}) requires a value but the cell is empty",
            )
        return None, None

    token = _normalize_token(raw)
    if token in mapping.true_values:
        return True, None
    if token in mapping.false_values:
        return False, None
    return None, RejectionError(
        kind="invalid_nullable",
        sheet_row=sheet_row,
        column=mapping.spec_name,
        value=raw,
        message=(
            f"nullable raw value {raw!r} is not in the configured true/false set"
        ),
    )
