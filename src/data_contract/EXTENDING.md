# Extending data_contract

This package has three plugin registries. To add a new check or metric,
pick the tier, drop a module in the right folder, and the YAML loader will
expect it from the next run onwards.

| You want to... | Tier | Folder | Base class |
|---|---|---|---|
| Validate one field against a per-field rule (regex, range, allowed values) | `field` | `field_constraints/` | `FieldConstraint` |
| Validate a whole-table or cross-table invariant (uniqueness, FK, row-count check, schema gap) | `table` | `table_checks/` | `TableCheck` |
| Profile the data (null %, distinct, completeness, custom statistic) | `field` or `table` | `metrics/` | `TableMetric` |

The fourth tier (`structural`: type_coercion, nullable, max_length) is
hard-coded inside `validation/checks/core_fields.py` and IS the type
system. New structural checks belong in `configs/types.yaml` (new `Type`
entry), not here.

---

## Adding a field constraint

Spec-driven, configurable per field via `column_mapping` in an epic's
`defaults.yaml`.

1. Drop a new module under [`field_constraints/`](field_constraints/).
2. Subclass `FieldConstraint`. Declare:
   - `name` — short identifier used in `validation.yaml`.
   - `contract_key` — how it appears in the generated contract YAML
     (usually same as `name`).
   - `VIOLATION_KIND` — the `Violation.kind` your data-side check emits.
   - `CONTRACT_VALUE_SCHEMA` — JSON Schema fragment for the value shape.
   - Docstring with `Spec cell:`, `Contract output:`, `Drift:` sections
     (the registry enforces this).
3. Implement `_parse_non_empty(raw_str, raw_original, ctx)` — spec cell to
   contract value.
4. Optionally implement `check_data(frame, field, check)` — return a
   Polars `LazyFrame` of violating rows.
5. Implement `diff(field_name, old, new)` — return a `DriftChange` or
   `None`.
6. Add the module path to `_BUILTIN_MODULES` in
   [`field_constraints/__init__.py`](field_constraints/__init__.py).
7. Reference its `name` under `checks.field:` in your `validation.yaml`.

Minimal example: see
[`field_constraints/pattern.py`](field_constraints/pattern.py).

```python
from data_contract import FieldConstraint

class MyConstraint(FieldConstraint):
    """Field length is divisible by 4.

    Spec cell:    OUI/NON-style boolean.
    Contract output: flat `divisible_by_4: true`.
    Drift:        added = breaking; removed = additive.
    """
    name = "divisible_by_4"
    contract_key = "divisible_by_4"
    VIOLATION_KIND = "divisible_by_4_violation"
    # ... _parse_non_empty, check_data, diff
```

---

## Adding a table check

Whole-table or cross-table invariant.

1. Drop a new module under [`table_checks/`](table_checks/).
2. Subclass `TableCheck`. Declare:
   - `name` — short identifier.
   - `description` — one-line description.
   - `VIOLATION_KIND` — emitted `Violation.kind`.
   - `DIMENSION` — one of `completeness`, `validity`, `uniqueness`,
     `consistency` (drives the quality score grid).
   - `scope` — `"row"` (returns a `LazyFrame` of offending rows) or
     `"table"` (returns a `list[Violation]` directly, for schema gaps
     like a missing column).
   - `requires_cross_table = True` if you need parent-table frames; the
     runner schedules these in a post-all-tables phase.
3. Implement
   `check_data(self, frame, contract, *, data_columns, contracts_by_table, table_frames)`.
   Return `None`, a `LazyFrame`, or `list[Violation]`.
4. Add the module path to `_BUILTIN_MODULES` in
   [`table_checks/__init__.py`](table_checks/__init__.py).
5. Reference its `name` under `checks.table:` in your `validation.yaml`.

Example: see [`table_checks/pk_uniqueness.py`](table_checks/pk_uniqueness.py)
(row-scope) or
[`table_checks/column_missing.py`](table_checks/column_missing.py)
(table-scope).

---

## Adding a metric

Descriptive statistic surfaced on the report.

1. Drop a new module under [`metrics/`](metrics/).
2. Subclass `TableMetric`. Declare:
   - `name` — short identifier.
   - `description` — one-line description.
   - `scope` — `"field"` (returns one value per contract field) or
     `"table"` (returns a single scalar; values key is `"__table__"`).
3. Implement `compute(self, df, contract, type_registry) -> MetricResult`.
   Return `MetricResult(name=self.name, scope=self.scope, values={...})`.
   Polars must be lazy-imported inside the method body so the package
   stays Polars-free at import time.
4. Add the module path to `_BUILTIN_MODULES` in
   [`metrics/__init__.py`](metrics/__init__.py).
5. Reference its `name` under `metrics.field:` or `metrics.table:` in
   your `validation.yaml`, matching the metric's `scope`.

Example: see [`metrics/null_count.py`](metrics/null_count.py) (field
scope) or [`metrics/row_count.py`](metrics/row_count.py) (table scope).

---

## After adding an extension

1. Restart any long-running process — registries populate at import time.
2. Re-run the validation; the YAML loader will require an explicit
   `true|false` for your new name in the matching tier.
3. Add a test under
   [`tests/field_constraints/`](../../tests/field_constraints/),
   [`tests/table_checks/`](../../tests/table_checks/), or
   [`tests/metrics/`](../../tests/metrics/) following the registry-test
   pattern next to existing fixtures.

The CLI subcommands that consume your extension (`generate`,
`validate-contract`, `validate-data`) don't need any wiring — registry
dispatch handles everything.
