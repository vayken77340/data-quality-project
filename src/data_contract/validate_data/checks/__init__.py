"""Non-constraint data checks.

Per-constraint check logic lives ON the constraint class via
`FieldConstraint.check_data` (see `field_constraints/`).

This subpackage handles checks that aren't tied to a registered constraint:

  - `core_fields`: nullable, max_length, type coercion, precision/scale
  - `keys`:        PK uniqueness, FK existence across tables
"""
