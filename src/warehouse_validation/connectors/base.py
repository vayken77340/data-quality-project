"""Connector ABC. Read-only scalar/count/columns surface.

Silver pushdown is `SELECT COUNT(*) FROM <t> WHERE <predicate>` --
served by `execute_scalar` / `execute_count`. Bronze fidelity adds
`execute_columns` to discover the bronze table's column set so the
runner can compare it against the contract field set. Reconciliation
reuses `execute_count`. No transactions, no pooling, no row pulls --
those land if a future phase actually needs them.
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

    @abstractmethod
    def execute_columns(self, fq_table: str) -> set[str]:
        """Return the set of column names in `fq_table`.

        Accepts either a fully-qualified `<catalog>.<schema>.<table>`
        name or a bare `<table>` name (resolved against the connection's
        default catalog/schema). Case-sensitive: names are returned
        exactly as the warehouse stores them.
        """
