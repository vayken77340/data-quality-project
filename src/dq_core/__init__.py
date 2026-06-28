"""dq_core -- shared primitives for data_contract and warehouse_validation.

Holds the contract domain model (Contract / FieldContract / FieldCheck),
the three plugin registries (field_constraints, table_checks, metrics),
the violation + report data model, and every report writer
(JSON / HTML / Markdown / XLSX). Contract-agnostic in the sense that it
knows nothing about Excel specs or warehouse drivers; it knows the
FieldContract shape because that is the cross-package interface.

Depends on neither data_contract nor warehouse_validation; both depend
on this package.
"""

from __future__ import annotations
