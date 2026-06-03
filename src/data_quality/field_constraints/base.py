from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field as dc_field
from typing import Any, ClassVar

from data_quality.errors import ConfigError, RejectionError
from data_quality.type_mapping import Type


# ---------------------------------------------------------------------------
# Shared dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConstraintContext:
    sheet_row: int
    field_type: Type
    field_max_length: int | None


@dataclass(frozen=True)
class DriftChange:
    kind: str
    severity: str  # "breaking" | "additive" | "cosmetic"
    field: str | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "severity": self.severity}
        if self.field is not None:
            out["field"] = self.field
        for k, v in self.detail.items():
            out[k] = v
        return out


@dataclass
class ConstraintColumnRef:
    """The spec-column lookup info shared by every constraint.

    `required` (default True): the column header must exist in the sheet.
    `value_required` (default False): every non-empty row must have a value.
    """
    spec_name: str
    required: bool = True
    value_required: bool = False


# ---------------------------------------------------------------------------
# Config helpers (referenced by FieldConstraint.from_config)
# ---------------------------------------------------------------------------


def parse_column_ref(raw: dict, *, name: str) -> ConstraintColumnRef:
    spec_name = raw.get("spec_name")
    if not isinstance(spec_name, str) or not spec_name:
        raise ConfigError(f"column_mapping.{name}.spec_name must be a non-empty string")
    required = bool(raw.get("required", True))
    value_required = bool(raw.get("value_required", False))
    if not required and value_required:
        raise ConfigError(
            f"column_mapping.{name}: cannot have `required: false` with `value_required: true`. "
            f"A column whose existence is optional cannot also require values per row."
        )
    return ConstraintColumnRef(spec_name=spec_name, required=required, value_required=value_required)


DEFAULT_BOOL_TRUE = frozenset(["oui", "yes", "true", "1", "o", "y"])
DEFAULT_BOOL_FALSE = frozenset(["non", "no", "false", "0", "n"])


def load_bool_values(raw: dict, *, name: str) -> tuple[frozenset[str], frozenset[str]]:
    """Parse an optional `values:` block for a bool constraint."""
    block = raw.get("values")
    if block is None:
        return DEFAULT_BOOL_TRUE, DEFAULT_BOOL_FALSE
    if not isinstance(block, dict):
        raise ConfigError(f"column_mapping.{name}.values must be a mapping with 'true' / 'false' lists")
    true_list = block.get("true") or block.get(True) or []
    false_list = block.get("false") or block.get(False) or []
    if not isinstance(true_list, list) or not isinstance(false_list, list):
        raise ConfigError(f"column_mapping.{name}.values 'true' / 'false' must be lists")
    true_set = frozenset(str(v).strip().lower() for v in true_list)
    false_set = frozenset(str(v).strip().lower() for v in false_list)
    overlap = true_set & false_set
    if overlap:
        raise ConfigError(
            f"column_mapping.{name}.values: tokens appear under both true and false: {sorted(overlap)}"
        )
    return true_set, false_set


def parse_bool(
    raw: object | None,
    true_values: frozenset[str],
    false_values: frozenset[str],
) -> tuple[bool | None, str | None]:
    """Public helper for custom constraints that want OUI/NON-style parsing on a
    side input. Returns (bool, None) on success; (None, '') on a blank cell;
    (None, normalized_token) on an unrecognized value.
    """
    if raw is None or str(raw).strip() == "":
        return None, ""
    token = str(raw).strip().lower()
    if token in true_values:
        return True, None
    if token in false_values:
        return False, None
    return None, token


# ---------------------------------------------------------------------------
# Drift helpers shared by every constraint's `diff()` method.
# ---------------------------------------------------------------------------


def diff_added_or_removed(
    field_name: str,
    old: Any | None,
    new: Any | None,
    *,
    added_kind: str,
    added_severity: str,
    removed_kind: str,
    removed_severity: str,
) -> DriftChange | None:
    """Common prefix for diff methods: classify the (None, value) and
    (value, None) transitions. Returns None when both sides have values (caller
    handles the value-changed case) or when both are None.
    """
    if old is None and new is not None:
        return DriftChange(
            kind=added_kind,
            severity=added_severity,
            field=field_name,
            detail={"to": new},
        )
    if new is None and old is not None:
        return DriftChange(
            kind=removed_kind,
            severity=removed_severity,
            field=field_name,
            detail={"from": old},
        )
    return None


def diff_numeric_bound(
    field_name: str,
    old: Any | None,
    new: Any | None,
    *,
    name: str,            # used for the kind prefix: "{name}_raised" / "_lowered" / "_changed"
    raise_severity: str,  # severity when new > old
    lower_severity: str,  # severity when new < old
) -> DriftChange | None:
    """Diff for a comparable scalar bound (min_value, max_value). Direction of
    "tighter" is encoded by the two severity args: for `min_value`, raising the
    bound is breaking; for `max_value`, raising it is additive.
    """
    if old == new:
        return None
    edge = diff_added_or_removed(
        field_name, old, new,
        added_kind=f"{name}_added", added_severity="breaking",
        removed_kind=f"{name}_removed", removed_severity="additive",
    )
    if edge is not None:
        return edge
    try:
        is_raised = new > old
    except TypeError:
        return DriftChange(
            kind=f"{name}_changed",
            severity="breaking",
            field=field_name,
            detail={"from": old, "to": new},
        )
    return DriftChange(
        kind=f"{name}_raised" if is_raised else f"{name}_lowered",
        severity=raise_severity if is_raised else lower_severity,
        field=field_name,
        detail={"from": old, "to": new},
    )


# ---------------------------------------------------------------------------
# Constraint base classes
# ---------------------------------------------------------------------------


class FieldConstraint(ABC):
    name: ClassVar[str] = ""
    contract_key: ClassVar[str] = ""

    column: ConstraintColumnRef
    raw_config: dict[str, Any]

    def __init__(self, column: ConstraintColumnRef, **_unused: Any) -> None:
        # Subclasses with extra state override `__init__` and call super().__init__(column).
        self.column = column
        self.raw_config = {}

    # -- config -------------------------------------------------------------

    @classmethod
    def from_config(cls, raw: dict) -> "FieldConstraint":
        """Default implementation. Override only when extra constructor kwargs
        aren't expressible via `_extra_init_args` (rare)."""
        column = parse_column_ref(raw, name=cls.name)
        instance = cls(column=column, **cls._extra_init_args(raw))
        instance.raw_config = dict(raw)
        return instance

    @classmethod
    def _extra_init_args(cls, raw: dict) -> dict[str, Any]:
        """Hook for subclasses to inject extra kwargs into `__init__` from the
        YAML config block. Default: none."""
        return {}

    # -- cell parsing -------------------------------------------------------

    def parse_cell(
        self,
        raw: object | None,
        ctx: ConstraintContext,
    ) -> tuple[Any | None, RejectionError | None]:
        """Template method: every constraint shares the same blank-cell handling.

        Blank + value_required  -> missing_mandatory rejection.
        Blank + optional        -> (None, None); the field omits this key in the contract.
        Otherwise               -> dispatches to `_parse_non_empty` with the trimmed string.
        """
        if raw is None or str(raw).strip() == "":
            if self.column.value_required:
                return None, self._missing_mandatory(ctx)
            return None, None
        return self._parse_non_empty(str(raw).strip(), raw, ctx)

    @abstractmethod
    def _parse_non_empty(
        self,
        raw_str: str,
        raw_original: object,
        ctx: ConstraintContext,
    ) -> tuple[Any | None, RejectionError | None]:
        """Implement constraint-specific parsing. `raw_str` is the trimmed string,
        guaranteed non-empty. `raw_original` is the untouched cell value
        (preserved so rejection messages can quote it verbatim).
        """

    # -- drift --------------------------------------------------------------

    @classmethod
    @abstractmethod
    def diff(cls, field_name: str, old: Any | None, new: Any | None) -> DriftChange | None: ...

    # -- shared rejection builders -----------------------------------------

    def _missing_mandatory(self, ctx: ConstraintContext) -> RejectionError:
        return RejectionError(
            kind="missing_mandatory",
            sheet_row=ctx.sheet_row,
            column=self.column.spec_name,
            field=self.name,
            message=(
                f"field {self.name!r} (column {self.column.spec_name!r}) "
                f"requires a value but the cell is empty"
            ),
        )

    def _reject(
        self,
        kind: str,
        ctx: ConstraintContext,
        *,
        raw: object,
        message: str,
    ) -> RejectionError:
        return RejectionError(
            kind=kind,
            sheet_row=ctx.sheet_row,
            column=self.column.spec_name,
            field=self.name,
            value=raw,
            message=message,
        )


class _BoolConstraint(FieldConstraint):
    """Shared implementation for boolean constraints (primary_key, unique).

    A `true` cell emits the constraint into the contract as `true`;
    a `false` cell is omitted (the default state is "not set"), so the
    contract stays terse.
    """

    def __init__(
        self,
        column: ConstraintColumnRef,
        true_values: frozenset[str],
        false_values: frozenset[str],
    ) -> None:
        super().__init__(column)
        self.true_values = true_values
        self.false_values = false_values

    @classmethod
    def _extra_init_args(cls, raw: dict) -> dict[str, Any]:
        true_v, false_v = load_bool_values(raw, name=cls.name)
        return {"true_values": true_v, "false_values": false_v}

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        token = raw_str.lower()
        if token in self.true_values:
            return True, None
        if token in self.false_values:
            return None, None  # false is the default state; omit from the contract
        return None, self._reject(
            "invalid_bool",
            ctx,
            raw=raw_original,
            message=(
                f"{self.name!r} raw value {raw_original!r} is not in the "
                f"configured true/false set"
            ),
        )
