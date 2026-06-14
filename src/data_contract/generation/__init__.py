"""data_contract.generation -- spec to contract pipeline.

This subpackage groups every module involved in turning an Excel spec into
a contract YAML:

* `spec_reader` -- open the workbook, discover sheets, iterate field rows.
* `header_matcher` -- normalise spec column headers for lookup.
* `config` -- spec-format configuration (column_mapping, keys/joins sheet
  shape, specs_parsing.yaml). Note: this is NOT the validation
  config; the validation side has its own `data_contract.validation.config`.
* `nullable`, `keys`, `joins` -- per-shape parsers.
* `drift` -- diff two contract YAMLs (breaking / additive / cosmetic).
* `schema_export` -- emit JSON Schema for the contract format.
* `validate_contract` -- syntactic + semantic validation of contract YAMLs.
* `catalog` -- regenerate docs/constraints.md from the field_constraints registry.
* `docs` -- per-epic XLSX data dictionary generator.

The companion subpackage is `data_contract.validation`, which takes the
contracts produced here and runs them against sample data.
"""
