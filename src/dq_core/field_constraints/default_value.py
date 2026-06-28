"""The `default_value` constraint: a per-field default coerced against the field's declared type.

The constraint name (`default_value`) differs from its contract_key (`default`)
to keep the spec column name readable while keeping the contract terse.
"""

from __future__ import annotations

from dq_core.errors import ConfigError
from dq_core.field_constraints._typed_value import parse_typed_value
from dq_core.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


class DefaultValueConstraint(FieldConstraint):
    """Declares the default value a downstream consumer should use when the field is missing.

    Spec cell:    a typed value, coerced against the field's declared type via parse_typed_value;
                  cells matching `spec_parsing.null_tokens` (e.g. "-", "n/a") are treated as blank.
    Contract output: flat scalar `default: <value>` (no params).
    Drift:        added/changed = breaking; removed = additive.
    """

    name = "default_value"
    contract_key = "default"

    SPEC_PARSING_FIELDS = ("null_tokens",)
    CONTRACT_FIELDS = ()

    # Permissive: the contract carries whatever typed value parse_typed_value
    # coerced, which depends on the field's declared type. Schema doesn't
    # constrain.
    CONTRACT_VALUE_SCHEMA = {}

    null_tokens: frozenset[str]

    def _configure(self) -> None:
        tokens = self._spec_parsing_params.get("null_tokens", [])
        if not isinstance(tokens, list):
            raise ConfigError(
                f"column_mapping.{self.name}.spec_parsing.null_tokens must be a list of strings"
            )
        self.null_tokens = frozenset(str(t).strip().lower() for t in tokens)

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        if raw_str.strip().lower() in self.null_tokens:
            return None, None
        value, err_msg = parse_typed_value(raw_str, ctx.field_type)
        if err_msg is not None:
            return None, self._reject(
                "invalid_default_value", ctx, raw=raw_original,
                message=f"default_value: {err_msg}",
            )
        return value, None

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        if old == new:
            return None
        edge = diff_added_or_removed(
            field_name, old, new,
            added_kind="default_added", added_severity="breaking",
            removed_kind="default_removed", removed_severity="additive",
        )
        if edge is not None:
            return edge
        return DriftChange(
            kind="default_changed",
            severity="breaking",
            field=field_name,
            detail={"from": old, "to": new},
        )
