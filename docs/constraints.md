# Field constraints — catalog and conventions

This page is the source of truth for everything about field constraints in this
generator. The README links here. As constraints land, update the catalog table
and the examples; do not let it go stale.

## 1. Catalog of built-ins

The table below is auto-generated from the constraint registry + each
constraint's docstring. Run `python -m data_contract regen-docs` to refresh it;
`lint` fails if the file is out of date.

<!-- BEGIN AUTO-GENERATED CATALOG -->

| Name | Contract key | Spec cell | Contract output | Drift |
|---|---|---|---|---|
| `allowed_values` | `allowed_values` | delimited string (separator configurable, default `|`). | flat list `allowed_values: [...]`. | values added = additive; values removed = breaking. |
| `default_value` | `default` | a typed value, coerced against the field's declared type via parse_typed_value; cells matching `spec_parsing.null_tokens` (e.g. "-", "n/a") are treated as blank. | flat scalar `default: <value>` (no params). | added/changed = breaking; removed = additive. |
| `format` | `format` | a case-insensitive token registered in FORMAT_REGISTRY. | flat string `format: <token>`. | added/changed = breaking; removed = additive. |
| `max_value` | `max_value` | a typed value (coerced via `parse_typed_value`); for string fields, treated as a length cap. | structured `max_value: {value, strict}`; `strict: false` means `<=`, `true` means `<`. | value lowered = breaking; value raised = additive; strict tightened (<= -> <) = breaking; strict loosened = additive. |
| `min_value` | `min_value` | a typed value (coerced via `parse_typed_value` against the field's declared type). | structured `min_value: {value, strict}`; `strict: false` means `>=`, `true` means `>`. | value raised = breaking; value lowered = additive; strict tightened (>= -> >) = breaking; strict loosened = additive. |
| `pattern` | `pattern` | a regex string; validated by `re.compile` at parse time. | flat string `pattern: <regex>`. | added/changed = breaking; removed = additive. |
| `unique` | `unique` | OUI/NON-style boolean token (configurable via `values:` block). | flat `unique: true`; false is omitted from the contract. | added = breaking; removed = additive. |

<!-- END AUTO-GENERATED CATALOG -->

The format registry ships with the following tokens. Extend via
`register_format(name, description=..., pattern=...)`.

<!-- BEGIN AUTO-GENERATED FORMATS -->

| Token | Description |
|---|---|
| `email` | RFC 5322 email address |
| `iban` | IBAN account number (country prefix + check digits + BBAN) |
| `phone` | E.164 international phone number |
| `uuid` | RFC 4122 UUID (any version, hyphenated) |

<!-- END AUTO-GENERATED FORMATS -->

## 2. Sub-block conventions

Every constraint entry under `fields.column_mapping` may declare two optional
sub-blocks. The split makes the intent unambiguous and is enforced at config
load (unknown keys raise `ConfigError`):

- **`spec_parsing:`** — knobs consumed only by the generator while parsing the
  spec cell. They never appear in the contract output. Examples: the separator
  for `allowed_values`, the null-token list for `default_value`.
- **`contract_params:`** — knobs that flow into the contract output, sitting
  alongside the per-field value so the downstream validator knows how to check
  the data. Examples: `strict` on `min_value`/`max_value`.

A constraint declares which keys it accepts in each block via class-level
`SPEC_PARSING_FIELDS` and `CONTRACT_FIELDS` tuples. Unknown keys are rejected
at `from_config` time.

## 3. Structured-shape decision (recorded)

The base class's `to_contract_value` emits `{value, **contract_params}` when
`CONTRACT_FIELDS` is non-empty. This is the default shape for the 90% case
(one parsed value + N runtime knobs). **Constraints whose natural shape doesn't
fit override `to_contract_value` directly** and document the override in the
class docstring.

Four shape patterns to expect:

1. **Flat** (current `allowed_values`, `pattern`, `unique`, `format`,
   `default_value`) — empty `CONTRACT_FIELDS`; the helper returns
   `parsed_value` unchanged.
2. **One-value + params** (current `min_value`, `max_value`) — non-empty
   `CONTRACT_FIELDS`; helper auto-wraps to `{value, <params>}`.
3. **Two-value** (hypothetical `numeric_range`) — override
   `to_contract_value` to emit `{low, high, strict_low, strict_high}` directly.
4. **Params-only** (hypothetical epic-wide `sensitivity`) — override to emit
   `{level: <token>}`, no `value:` key.

The escape hatch (override `to_contract_value`) means we never need to refactor
the base class when a new shape arrives — the new constraint just opts out of
the default wrapping.

## 4. Docstring requirements

Every `FieldConstraint` subclass MUST include a docstring whose body contains
these three labeled lines (the `register()` call rejects classes that don't):

```
Spec cell:        <what the generator reads from the cell>
Contract output:  <what shape lands in the YAML>
Drift:            <severity policy for the changes this constraint can produce>
```

Existing constraints all follow this template; copy from `allowed_values.py`
or `min_value.py` when adding a new one.

## 5. How to add a constraint

1. Drop a new module under `src/dq_core/field_constraints/`.
2. Subclass `FieldConstraint` (or `_BoolConstraint` for OUI/NON-style booleans).
3. Set class-level `name`, `contract_key`, `SPEC_PARSING_FIELDS`,
   `CONTRACT_FIELDS`, plus the docstring with the three required headers.
4. Implement `_parse_non_empty(self, raw_str, raw_original, ctx)` returning
   `(value, error_or_None)`.
5. Implement `diff(cls, field_name, old, new)` returning a `DriftChange` or
   `None`. Reuse `diff_added_or_removed` / `diff_numeric_bound` from
   `base.py` where they fit.
6. Add the module path to `_BUILTIN_MODULES` in
   `field_constraints/__init__.py`.
7. Write tests covering: parse success, parse rejection kinds, drift for each
   transition (added/removed/changed), and any sub-block ConfigError paths.
8. Add a row to the catalog table at the top of this file.
9. If the constraint output shape is anything other than "flat" or "one-value +
   params", override `to_contract_value` and document the shape in the
   docstring.

## 6. What's deliberately out of scope

- **Multi-column-per-constraint** (e.g. one constraint reading `Range Low` +
  `Range High` as separate columns). The current ColumnMapping ties one
  constraint to one column. If a logical concept truly needs two columns,
  prefer collapsing them into one column with a parse convention (e.g.
  `0..100`) inside the constraint.
- **Cross-field rules** (e.g. `end_date >= start_date`). These don't fit the
  per-cell model and belong on a separate spec sheet parallel to keys/joins.
  Not implemented yet.
- **Cross-table rules** (sums, FK existence in real data). These belong to the
  downstream data validator, not contract generation.
