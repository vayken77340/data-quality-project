"""Violation dataclass: the validator's primary output primitive."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Violation:
    """A single failed check observation on a (table, field, source row)."""
    kind: str                                   # e.g. "nullable_violation", "pk_not_unique"
    severity: str                               # "error" | "warning" | "info"
    table: str
    field: str | None = None                    # None for table-level violations
    source_file: str | None = None              # None for cross-file violations
    source_row: int | None = None               # 1-based within source_file
    pk_values: dict[str, Any] | None = None     # offending row's PK columns (when declared)
    offending_value: Any = None
    expected: str = ""                          # plain-language description of the rule

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "severity": self.severity,
            "table": self.table,
        }
        if self.field is not None:
            out["field"] = self.field
        if self.source_file is not None:
            out["source_file"] = self.source_file
        if self.source_row is not None:
            out["source_row"] = self.source_row
        if self.pk_values is not None:
            out["pk_values"] = dict(self.pk_values)
        if self.offending_value is not None:
            out["offending_value"] = self.offending_value
        if self.expected:
            out["expected"] = self.expected
        return out

    def render(self) -> str:
        parts: list[str] = [self.kind]
        if self.field:
            parts.append(f"field={self.field!r}")
        if self.source_file:
            row_part = f":row {self.source_row}" if self.source_row is not None else ""
            parts.append(f"at {self.source_file}{row_part}")
        if self.offending_value is not None:
            parts.append(f"got={self.offending_value!r}")
        if self.expected:
            parts.append(f"expected={self.expected}")
        return " | ".join(parts)
