from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_contract.errors import ConfigError, RejectionError


# Sentinel meaning "no default_value declared" -- same shape as
# `generation.config._UNSET` and `field_constraints.base._UNSET`.
_UNSET: Any = object()


@dataclass(frozen=True)
class NullableMapping:
    spec_name: str
    true_values: frozenset[str]
    false_values: frozenset[str]
    column_required: bool = True
    default_value: Any = _UNSET

    @property
    def has_default(self) -> bool:
        return self.default_value is not _UNSET

    @classmethod
    def from_dict(cls, raw: dict) -> "NullableMapping":
        spec_name = raw.get("spec_name")
        if not isinstance(spec_name, str) or not spec_name:
            raise ConfigError("column_mapping.nullable.spec_name must be a non-empty string")
        column_required = bool(raw.get("column_required", True))
        default_value: Any = raw["default_value"] if "default_value" in raw else _UNSET
        if not column_required and default_value is _UNSET:
            raise ConfigError(
                "column_mapping.nullable: `column_required: false` requires "
                "`default_value` to be declared. Set `default_value: null` "
                "if the nullable field should be omitted from the contract "
                "on blanks."
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
            column_required=column_required,
            default_value=default_value,
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
      (True/False, None)             on a recognized value
      (default_value, None)          on empty + `mapping.has_default`. The
                                     default is used as-is; it does NOT
                                     go through token normalisation
                                     (declare bools as `true`/`false` or
                                     null).
      (None, missing_mandatory)      on empty + no default declared
      (None, invalid_nullable)       on an unrecognized non-empty token
    """
    is_empty = raw is None or str(raw).strip() == ""
    if is_empty:
        if mapping.has_default:
            return mapping.default_value, None
        return None, RejectionError(
            kind="missing_mandatory",
            sheet_row=sheet_row,
            column=mapping.spec_name,
            field="nullable",
            message=(
                f"field 'nullable' (column {mapping.spec_name!r}) requires "
                f"a value but the cell is empty. Declare `default_value` "
                f"on the nullable column in specs_parsing.yaml to make "
                f"blanks acceptable."
            ),
        )

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
