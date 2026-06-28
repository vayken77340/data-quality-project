"""Base classes for the pluggable table-check registry.

A `TableCheck` runs against a single table's frame (and, for cross-table
checks, the other tables' frames too). It emits one `Violation` per
offending row (row-scope) or per offending occurrence (table-scope, e.g.
"this column is missing from the data file").

Three things differentiate this tier from `FieldConstraint`:

1. The check operates on the whole frame, not on a single field's column.
2. There is no per-field config in `column_mapping` -- the check is either
   on or off via the validation.yaml `checks:` block.
3. Some checks need parent-table frames (FK existence). Those declare
   `requires_cross_table = True` so the runner schedules them in the
   post-all-tables phase.

Adding a new table check:
1. Drop a new module under `dq_core/table_checks/` defining a
   `TableCheck` subclass.
2. Register it in `_BUILTIN_MODULES` in `__init__.py` (or call
   `register(MyCheck)` manually from another package).
3. Add an on/off entry under `checks.table` in `validation.yaml`. The
   parser pulls valid names from this registry at load time, so adding
   a check immediately makes the YAML expect it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from dq_core.violations import Violation


class TableCheck(ABC):
    """Pluggable whole-table check.

    ClassVars every subclass must declare:
      - `name`            -- short identifier used in validation.yaml.
      - `description`     -- one-line human-readable description.
      - `VIOLATION_KIND`  -- the `Violation.kind` emitted by this check.
      - `DIMENSION`       -- DAMA dimension this check feeds into
                             ("completeness" | "validity" | "uniqueness"
                              | "consistency"). Drives report scoring.

    Optional ClassVars:
      - `VIOLATION_SEVERITY`     -- default "error".
      - `scope`                  -- "row" (default; returns a LazyFrame of
                                    violating rows) or "table" (returns a
                                    list[Violation] directly, for schema
                                    gaps like a missing column).
      - `requires_cross_table`   -- default False. When True, the runner
                                    schedules this check in the post-all-
                                    tables phase and passes parent frames
                                    via `contracts_by_table` / the
                                    `table_frames` arg on `check_data`.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    VIOLATION_KIND: ClassVar[str] = ""
    VIOLATION_SEVERITY: ClassVar[str] = "error"
    DIMENSION: ClassVar[str] = ""
    scope: ClassVar[str] = "row"
    requires_cross_table: ClassVar[bool] = False

    @abstractmethod
    def check_data(
        self,
        frame,
        contract,
        *,
        data_columns: set[str] | None = None,
        contracts_by_table: dict[str, Any] | None = None,
        table_frames: dict[str, Any] | None = None,
    ):
        """Return one of:

        * `None`              -- no violations.
        * a Polars LazyFrame  -- one row per offending source row (for
                                 `scope="row"`). The runner will turn each
                                 row into a `Violation` keyed by
                                 (`source_file`, `__row_index__`).
        * `list[Violation]`   -- direct violations (for `scope="table"`,
                                 e.g. column_missing where there is no
                                 source row to point at).

        `data_columns` is the set of columns actually present in the data
        frame (without the helper `__source_file__` / `__row_index__`
        columns). `column_missing` needs this; row-scope checks usually
        don't.

        `contracts_by_table` / `table_frames` are populated only for
        cross-table checks (`requires_cross_table=True`).
        """
