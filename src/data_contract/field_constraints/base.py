from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field as dc_field
from typing import Any, ClassVar

from data_contract.core.column_ref import (
    UNSET as _UNSET,
    ColumnRef,
    parse_column_ref as _parse_core_column_ref,
)
from data_contract.errors import ConfigError, RejectionError
from data_contract.type_mapping import Type


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


# `ConstraintColumnRef` was the constraint-side copy of `ColumnRef` carrying
# the same three fields. Kept here as a back-compat alias so existing imports
# (`from data_contract.field_constraints.base import ConstraintColumnRef`)
# resolve, but the implementation is the shared `ColumnRef`.
ConstraintColumnRef = ColumnRef


# ---------------------------------------------------------------------------
# Config helpers (referenced by FieldConstraint.from_config)
# ---------------------------------------------------------------------------


def parse_column_ref(raw: dict, *, name: str) -> ColumnRef:
    """Constraint-side wrapper that adapts the shared core parser to the
    `column_mapping.<name>` error-message prefix."""
    return _parse_core_column_ref(raw, prefix="column_mapping", key=name)


def _parse_sub_block(
    raw: dict,
    block_key: str,
    allowed_fields: tuple[str, ...],
    *,
    name: str,
) -> dict[str, Any]:
    """Pull `raw[block_key]` (if present), validate every key is in the
    constraint's allowlist, return the dict. Missing block returns {}.
    """
    block = raw.get(block_key)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"column_mapping.{name}.{block_key} must be a mapping")
    unknown = sorted(set(block) - set(allowed_fields))
    if unknown:
        raise ConfigError(
            f"column_mapping.{name}.{block_key}: unknown keys {unknown}; "
            f"accepted keys for {name!r}: {list(allowed_fields)}"
        )
    return dict(block)


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


def unwrap_structured_value(payload: Any) -> tuple[Any, dict[str, Any]]:
    """Split a structured constraint payload into (value, params).

    - `None`               -> (None, {})
    - dict with `value:`   -> (payload["value"], <other keys>)
    - flat scalar / list   -> (payload, {})

    Accepts the legacy flat shape so old history snapshots written before a
    constraint started carrying contract_params still diff cleanly against the
    new structured form.
    """
    if payload is None:
        return None, {}
    if isinstance(payload, dict):
        value = payload.get("value")
        params = {k: payload[k] for k in payload if k != "value"}
        return value, params
    return payload, {}


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

    # Allowlists of keys that may appear under each sub-block in YAML.
    # Subclasses override to declare what they accept. Unknown keys raise
    # ConfigError at parse time. Keys in SPEC_PARSING_FIELDS are consumed by
    # the generator only; keys in CONTRACT_FIELDS are emitted into the
    # contract output via `to_contract_value`.
    SPEC_PARSING_FIELDS: ClassVar[tuple[str, ...]] = ()
    CONTRACT_FIELDS: ClassVar[tuple[str, ...]] = ()

    # Data-validation metadata. Subclasses that override `check_data` MUST set
    # VIOLATION_KIND to a non-empty string; `register()` enforces this.
    # VIOLATION_SEVERITY defaults to "error"; override to "warning" or "info"
    # if the data-side check should be informational.
    VIOLATION_KIND: ClassVar[str] = ""
    VIOLATION_SEVERITY: ClassVar[str] = "error"

    # JSON Schema fragment describing the shape of this constraint's value in
    # the contract YAML. Used by `schema_export` to build the published
    # contract schema. Constraints with dynamic content (e.g. enum sourced from
    # a registry) may override `contract_value_schema()` as a classmethod
    # instead. Default `None` becomes permissive `{}` at schema build time.
    CONTRACT_VALUE_SCHEMA: ClassVar[dict[str, Any] | None] = None

    @classmethod
    def contract_value_schema(cls) -> dict[str, Any]:
        """JSON Schema fragment for this constraint's value. Override when the
        fragment depends on runtime state (e.g. a registry snapshot)."""
        if cls.CONTRACT_VALUE_SCHEMA is None:
            return {}
        return dict(cls.CONTRACT_VALUE_SCHEMA)

    column: ConstraintColumnRef
    raw_config: dict[str, Any]
    _spec_parsing_params: dict[str, Any]
    _contract_params: dict[str, Any]

    def __init__(self, column: ConstraintColumnRef) -> None:
        self.column = column
        self.raw_config = {}
        self._spec_parsing_params = {}
        self._contract_params = {}

    # -- config -------------------------------------------------------------

    @classmethod
    def from_config(cls, raw: dict) -> "FieldConstraint":
        column = parse_column_ref(raw, name=cls.name)
        spec_parsing = _parse_sub_block(raw, "spec_parsing", cls.SPEC_PARSING_FIELDS, name=cls.name)
        contract_params = _parse_sub_block(raw, "contract_params", cls.CONTRACT_FIELDS, name=cls.name)
        instance = cls(column=column)
        instance.raw_config = dict(raw)
        instance._spec_parsing_params = spec_parsing
        instance._contract_params = contract_params
        instance._configure()
        return instance

    def _configure(self) -> None:
        """Hook for subclasses to read self._spec_parsing_params /
        self._contract_params into typed attributes. Default: no-op."""

    # -- contract emission --------------------------------------------------

    def to_contract_value(self, parsed_value: Any) -> Any:
        """Combine the per-field parsed value with this constraint's
        contract_params for emission into the YAML.

        - No contract_params  -> emit the parsed value as-is (flat).
        - Has contract_params -> wrap as {"value": parsed_value, **contract_params}.
        """
        if not self._contract_params:
            return parsed_value
        return {"value": parsed_value, **self._contract_params}

    # -- cell parsing -------------------------------------------------------

    def parse_cell(
        self,
        raw: object | None,
        ctx: ConstraintContext,
    ) -> tuple[Any | None, RejectionError | None]:
        """Template method: every constraint shares the same blank-cell handling.

        Blank cell behaviour:
          * `column.has_default` -> `(column.default_value, None)`. The
            default is used as-is; it does NOT go through `_parse_non_empty`
            (the spec author is declaring the canonical value they want).
          * No default declared   -> `(None, missing_mandatory)`. The spec
            author must add `default_value` in specs_parsing.yaml to make
            the column optional.

        Non-blank cells dispatch to `_parse_non_empty` with the trimmed string.
        """
        if raw is None or str(raw).strip() == "":
            if self.column.has_default:
                return self.column.default_value, None
            return None, self._missing_mandatory(ctx)
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

    # -- data validation ----------------------------------------------------

    @classmethod
    def check_data(cls, frame, field, check):
        """Return a Polars LazyFrame of rows that VIOLATE this constraint,
        or None if the constraint has no data-side check.

        Default: returns None. Subclasses override to implement.
        Implementations MUST lazy-import polars inside the method body so the
        base data_contract package stays Polars-free at import time.
        """
        return None

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

    The optional `values:` block (true/false token lists) lives at the top
    level of the constraint YAML, not under `spec_parsing:` — this is a
    legacy compatibility shape, and `load_bool_values` reads it directly.
    """

    true_values: frozenset[str]
    false_values: frozenset[str]

    def _configure(self) -> None:
        true_v, false_v = load_bool_values(self.raw_config, name=self.name)
        self.true_values = true_v
        self.false_values = false_v

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
