"""Structural-tier data checks (type system derived).

Per-constraint check logic lives ON each FieldConstraint subclass via
`check_data` (see `data_contract/field_constraints/`). Whole-table and
cross-table checks live in `data_contract/table_checks/`.

This subpackage carries only the structural tier -- the checks that are
derived from the contract's type system (`Type`, `nullable`, `max_length`)
and so don't have per-instance configuration or a registry:

  - `core_fields`:   type_coercion, boolean_coercion, nullable, max_length
  - `value_parsers`: shared helpers for typed parsing
"""
