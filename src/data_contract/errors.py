from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad YAML, missing keys, ambiguous version)."""


class SpecReaderError(Exception):
    """Raised for unrecoverable spec-reading problems (file missing, sheet missing)."""


@dataclass(frozen=True)
class RejectionError:
    kind: str
    message: str
    sheet_row: int | None = None
    column: str | None = None
    field: str | None = None
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.sheet_row is not None:
            out["sheet_row"] = self.sheet_row
        if self.column is not None:
            out["column"] = self.column
        if self.field is not None:
            out["field"] = self.field
        if self.value is not None:
            out["value"] = self.value
        out["message"] = self.message
        return out


REJECTION_KINDS = frozenset({
    "unknown_type",
    "missing_mandatory",
    "invalid_nullable",
    "duplicate_field",
    "multi_table_in_sheet",
    "header_not_found",
    "invalid_bool",
    "invalid_pattern",
    "list_empty",
    "invalid_min_max",
    "keys_sheet_not_found",
    "keys_sheet_ambiguous",
    "keys_missing_table",
    "unknown_pk_field",
    "unknown_foreign_key_target",
    "ambiguous_foreign_key_target",
    "duplicate_table_across_sheets",
    "joins_sheet_not_found",
    "joins_sheet_ambiguous",
    "unknown_join_table",
    "unknown_join_column",
    "invalid_join_type",
    "invalid_cardinality",
    "nullable_primary_key",
})


@dataclass
class ErrorCollector:
    errors: list[RejectionError] = field(default_factory=list)

    def add(self, err: RejectionError) -> None:
        if err.kind not in REJECTION_KINDS:
            raise ValueError(f"unknown rejection kind: {err.kind}")
        self.errors.append(err)

    def has_errors(self) -> bool:
        return bool(self.errors)
