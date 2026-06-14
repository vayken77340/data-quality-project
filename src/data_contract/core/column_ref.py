"""Spec-column lookup primitive shared by every config that names a sheet column.

Four call sites used to carry their own copy of this dataclass:

  * generation/config.py: ColumnSpec, SplitColumnSpec, CardinalityColumnSpec
  * field_constraints/base.py: ConstraintColumnRef

They differ only in the optional `separator` (split / cardinality variants).
Single home with one `_UNSET` sentinel.

`read_cell` is the shared "is this cell blank, what do I substitute?"
helper that both `generation/builder.py` and `FieldConstraint.parse_cell`
used to reimplement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_contract.errors import ConfigError, RejectionError


# Sentinel meaning "no default_value declared". Distinguishes "the spec
# author didn't set a default" (cell-blank -> missing_mandatory error)
# from "the spec author set the default to YAML null" (cell-blank -> None).
UNSET: Any = object()


@dataclass(frozen=True)
class ColumnRef:
    """The spec-column lookup info shared by every column-bound config.

    `column_required` (default True): the column header must exist in the sheet.
    `default_value`: when declared (any value, including YAML null), blank
        cells in this column are silently replaced with the default.
        When NOT declared (the `UNSET` sentinel), blank cells produce a
        `missing_mandatory` rejection. Logical rule: `column_required=False`
        REQUIRES `default_value` to be declared.
    """
    spec_name: str
    column_required: bool = True
    default_value: Any = UNSET

    @property
    def has_default(self) -> bool:
        return self.default_value is not UNSET

    def read_cell(
        self,
        raw: object | None,
        *,
        sheet_row: int,
        field_name: str,
    ) -> tuple[Any, RejectionError | None]:
        """Shared blank-cell handler.

        Returns `(trimmed string, None)` when a value is present.

        When the cell is blank:
          * `has_default` -> `(default_value, None)`. The default is used
            as-is; it does NOT go through any typed parser.
          * No default -> `(None, missing_mandatory rejection)`.
        """
        if raw is None or str(raw).strip() == "":
            if self.has_default:
                return self.default_value, None
            return None, RejectionError(
                kind="missing_mandatory",
                sheet_row=sheet_row,
                column=self.spec_name,
                field=field_name,
                message=(
                    f"field {field_name!r} (column {self.spec_name!r}) "
                    f"requires a value but the cell is empty. Declare "
                    f"`default_value` on this column in specs_parsing.yaml "
                    f"to make blanks acceptable."
                ),
            )
        return str(raw).strip(), None


@dataclass(frozen=True)
class SeparatedColumnRef(ColumnRef):
    """Column whose cell contains a list of items joined by a separator."""
    separator: str = "|"


@dataclass(frozen=True)
class CardinalityColumnRef(ColumnRef):
    """Cardinality column. Optional explicit `separator` constrains the divider
    between the two sides (e.g. `1 -> n` with separator `->`). When None, the
    parser falls back to its default permissive set (`:`, `->`, ` to `)."""
    separator: str | None = None


def parse_column_ref(raw: dict, *, prefix: str, key: str) -> ColumnRef:
    """Parse a `{spec_name, column_required, default_value}` block into a ColumnRef."""
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{prefix}.{key} must be a mapping with 'spec_name' "
            f"(and optionally 'column_required', 'default_value')"
        )
    spec_name = raw.get("spec_name")
    if not isinstance(spec_name, str) or not spec_name:
        raise ConfigError(f"{prefix}.{key}.spec_name must be a non-empty string")
    column_required = bool(raw.get("column_required", True))
    default_value: Any = raw["default_value"] if "default_value" in raw else UNSET
    if not column_required and default_value is UNSET:
        raise ConfigError(
            f"{prefix}.{key}: `column_required: false` requires `default_value` "
            f"to be declared. Otherwise every row in a missing column would "
            f"emit a `missing_mandatory` rejection. Set `default_value: null` "
            f"if the field should be omitted from the contract on blanks."
        )
    return ColumnRef(
        spec_name=spec_name,
        column_required=column_required,
        default_value=default_value,
    )


def parse_separated_column_ref(
    raw: dict, *, prefix: str, key: str, default_separator: str = "|",
) -> SeparatedColumnRef:
    """Parse a `{spec_name, separator, column_required, default_value}` block."""
    base = parse_column_ref(raw, prefix=prefix, key=key)
    separator = raw.get("separator", default_separator)
    if not isinstance(separator, str) or not separator:
        raise ConfigError(
            f"{prefix}.{key}.separator must be a non-empty string"
        )
    return SeparatedColumnRef(
        spec_name=base.spec_name,
        column_required=base.column_required,
        default_value=base.default_value,
        separator=separator,
    )


def parse_cardinality_column_ref(
    raw: dict, *, prefix: str, key: str,
) -> CardinalityColumnRef:
    """Parse a cardinality column block (optional separator, may be None)."""
    base = parse_column_ref(raw, prefix=prefix, key=key)
    separator = raw.get("separator")
    if separator is not None and (not isinstance(separator, str) or not separator):
        raise ConfigError(
            f"{prefix}.{key}.separator, if set, must be a non-empty string"
        )
    return CardinalityColumnRef(
        spec_name=base.spec_name,
        column_required=base.column_required,
        default_value=base.default_value,
        separator=separator,
    )
