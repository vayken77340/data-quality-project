"""Connector ABC. Read-only scalar/count surface.

Silver-conformance pushdown today is `SELECT COUNT(*) FROM <t> WHERE
<predicate>`. The base class exposes exactly what that needs --
`execute_scalar` for arbitrary one-cell reads, `execute_count` as the
typed convenience wrapper. No transactions, no connection pooling, no
`fetch_rows` -- those land in later phases when bronze fidelity /
reconciliation actually need them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Connector(ABC):
    """Read-only warehouse access. One instance = one CLI invocation."""

    @abstractmethod
    def execute_scalar(self, sql: str) -> int | float | str | None:
        """Run `sql`, return the first column of the first row, or None."""

    @abstractmethod
    def execute_count(self, sql: str) -> int:
        """Run `sql` (expected to be a COUNT query), return the count as int."""
