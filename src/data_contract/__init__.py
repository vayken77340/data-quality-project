"""data_contract -- spec to contract to validation framework.

Layout (verb-axis):

    data_contract/
    ├── generation/          spec -> contract pipeline
    ├── validation/          contract + data -> quality report
    ├── field_constraints/   per-field constraint registry (dev extension point)
    ├── table_checks/        whole-table check registry (dev extension point)
    ├── metrics/             data profiling registry (dev extension point)
    ├── contract.py          shared Contract / FieldContract / FieldCheck dataclasses
    ├── type_mapping.py      shared type system
    ├── targets.py           shared per-database type overlay
    ├── errors.py            shared exceptions
    ├── settings.py          shared runtime flags
    ├── _util.py             shared YAML I/O + timestamp helpers
    ├── cli.py               CLI dispatcher
    └── __main__.py          entry point

Devs adding a new check or metric should NOT need to read framework code.
The three extension registries each have a base class + a register helper
re-exported below so a new check is at most a 3-line import. See
EXTENDING.md (next to this file) for the per-tier recipe.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Shared core dataclasses.
from data_contract.contract import Contract, FieldContract, FieldCheck

# Extension points -- re-exported so devs write `from data_contract import X`.
from data_contract.field_constraints import (
    FieldConstraint,
    register as register_constraint,
)
from data_contract.table_checks import (
    TableCheck,
    register as register_table_check,
)
from data_contract.metrics import (
    MetricResult,
    TableMetric,
    register as register_metric,
)

__all__ = [
    "__version__",
    # Core
    "Contract", "FieldContract", "FieldCheck",
    # Field constraints
    "FieldConstraint", "register_constraint",
    # Table checks
    "TableCheck", "register_table_check",
    # Metrics
    "TableMetric", "MetricResult", "register_metric",
]
