"""The `format` constraint: a single dispatchable token registry.

Rather than ship one constraint module per semantic format (email, uuid, iban,
phone), this one constraint carries a token (e.g. `email`) into the contract.
The actual data-side validation lives in the downstream validator and dispatches
through `FORMAT_REGISTRY`.

To extend, call `register_format("npi", description="...")` at startup or from
a plugin module.
"""

from __future__ import annotations

from dataclasses import dataclass

from dq_core.errors import ConfigError
from dq_core.field_constraints.base import (
    DriftChange,
    FieldConstraint,
    diff_added_or_removed,
)


@dataclass(frozen=True)
class FormatToken:
    name: str
    description: str
    # Optional informational regex carried for downstream consumers (validator,
    # data dictionary). The generator does NOT compile or evaluate this — it
    # only verifies the token name is registered.
    pattern: str | None = None


FORMAT_REGISTRY: dict[str, FormatToken] = {}


def register_format(name: str, *, description: str, pattern: str | None = None) -> None:
    """Add a format token to the registry. Idempotent only when re-registering
    the same (description, pattern) tuple; otherwise raises ConfigError."""
    n = name.strip().lower()
    if not n:
        raise ConfigError("format token name must be a non-empty string")
    existing = FORMAT_REGISTRY.get(n)
    if existing is not None:
        if existing.description == description and existing.pattern == pattern:
            return  # idempotent re-register
        raise ConfigError(
            f"format token {n!r} is already registered with a different definition"
        )
    FORMAT_REGISTRY[n] = FormatToken(name=n, description=description, pattern=pattern)


# ---------------------------------------------------------------------------
# Built-in tokens — registered at module import.
# ---------------------------------------------------------------------------


register_format(
    "email",
    description="RFC 5322 email address",
    pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
)
register_format(
    "uuid",
    description="RFC 4122 UUID (any version, hyphenated)",
    pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)
register_format(
    "iban",
    description="IBAN account number (country prefix + check digits + BBAN)",
    pattern=r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$",
)
register_format(
    "phone",
    description="E.164 international phone number",
    pattern=r"^\+?[1-9][0-9]{1,14}$",
)


# ---------------------------------------------------------------------------
# Constraint class
# ---------------------------------------------------------------------------


class FormatConstraint(FieldConstraint):
    """Declares a semantic format (e.g. email, uuid) the field's values must satisfy.

    Spec cell:    a case-insensitive token registered in FORMAT_REGISTRY.
    Contract output: flat string `format: <token>`.
    Drift:        added/changed = breaking; removed = additive.
    """

    name = "format"
    contract_key = "format"

    SPEC_PARSING_FIELDS = ()
    CONTRACT_FIELDS = ()

    VIOLATION_KIND = "format_violation"

    @classmethod
    def contract_value_schema(cls):
        # Dynamic: the enum reflects whatever's registered at export time.
        return {"type": "string", "enum": sorted(FORMAT_REGISTRY)}

    @classmethod
    def check_data(cls, frame, field, check):
        """Flag rows whose value is non-null and doesn't match the format's regex.

        The token in `check.value` is looked up in FORMAT_REGISTRY; if the
        registered token has no `pattern` (informational-only token), this
        check is a no-op (returns None).
        """
        import polars as pl

        token = str(check.value)
        token_info = FORMAT_REGISTRY.get(token)
        if token_info is None or token_info.pattern is None:
            return None
        regex = token_info.pattern
        col = pl.col(field.silver_name).cast(pl.String, strict=False)
        return frame.filter(col.is_not_null() & ~col.str.contains(regex))

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        token = raw_str.strip().lower()
        if token not in FORMAT_REGISTRY:
            return None, self._reject(
                "unknown_format", ctx, raw=raw_original,
                message=(
                    f"format {raw_original!r} is not in the format registry; "
                    f"known formats: {sorted(FORMAT_REGISTRY)}"
                ),
            )
        return token, None

    @classmethod
    def diff(cls, field_name, old, new) -> DriftChange | None:
        if old == new:
            return None
        edge = diff_added_or_removed(
            field_name, old, new,
            added_kind="format_added", added_severity="breaking",
            removed_kind="format_removed", removed_severity="additive",
        )
        if edge is not None:
            return edge
        return DriftChange(
            kind="format_changed",
            severity="breaking",
            field=field_name,
            detail={"from": old, "to": new},
        )
