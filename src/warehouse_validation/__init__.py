"""warehouse_validation -- SQL-pushdown validator for warehouse tables.

Depends only on `dq_core/` and the standard library (plus optional
connector packages like `trino`). Does NOT depend on `data_contract/` --
the file-side validator is a peer package, not a parent.

Layout:

    warehouse_validation/
    |-- cli.py                  validate-warehouse subcommand
    |-- config.py               WarehouseValidationConfig dataclass
    |-- setup.py                prepare_run: contract load + connector resolve
    |-- runner.py               run_validate_warehouse: orchestrator
    |-- emit_sql.py             ValidationReport assembly + 4-format dump
    |-- connectors/             read-only warehouse access (Trino today)
    `-- sql_predicates/         SQL-fragment compilers per constraint

Phase 2 ships the silver-conformance slice: one connector (Trino), one
table at a time, three pushdown constraints (min_value, max_value,
allowed_values).
"""

from __future__ import annotations

__version__ = "0.1.0"
