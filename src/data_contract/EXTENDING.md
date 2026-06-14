# Extending data_contract

This package has four plugin registries. To add a new check, metric, or
parser, pick the tier, drop a module in the right folder, and the YAML
loader will expect it from the next run onwards.

| You want to... | Tier | Folder | Base class |
|---|---|---|---|
| Validate one field against a per-field rule (regex, range, allowed values) | `field` | `field_constraints/` | `FieldConstraint` |
| Validate a whole-table or cross-table invariant (uniqueness, FK, row-count check, schema gap) | `table` | `table_checks/` | `TableCheck` |
| Profile the data (null %, distinct, completeness, custom statistic) | `field` or `table` | `metrics/` | `TableMetric` |
| Read a new file format (parquet, jsonl, fixed-width, ...) | `parser` | `data_parsers/` | `FileParser` |

The structural tier (`type_coercion`, `nullable`, `max_length`) is
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

## Adding a parser

Custom file-format reader (e.g. parquet, jsonl, fixed-width). Defaults
for the new format live in `configs/parsers.yaml`, NOT in the class — the
class declares behaviour, the YAML declares values.

1. Drop a new module under [`data_parsers/`](data_parsers/).
2. Subclass `FileParser`. Declare:
   - `name`        — short identifier used in `validation.yaml` (`format: my_parser`).
   - `extensions`  — tuple of recognised suffixes, e.g. `(".parquet",)`.
   - `PARSER_PARAMS` — allowlist of accepted config keys (typo gate).
   - Docstring with `Spec params:`, `Reads via:`, `Multi-file:` headers
     (the registry enforces this).
   Do NOT declare a `DEFAULTS` classvar — defaults live in YAML only.
3. Implement
   `parse_file(self, path, *, table_name_hint=None) -> ParsedFile`.
   Return:
   - `ParsedFile.rows`: either an iterable of stringified row dicts
     (the simple path) or a `pl.LazyFrame` (the fast path for parsers
     with a native scan, e.g. `pl.scan_csv`, `pl.scan_parquet`).
   - `ParsedFile.schema`: optional `ParserSchema(column_names=..., column_types=...)`
     when the format self-describes (CSV header, Excel header, JSON
     report_header, Parquet schema). Leave as `None` for opaque formats.

   Do NOT implement `read()` -- it's concrete on the base class. The
   framework owns the cross-cutting bookkeeping (forcing every column
   to `pl.String`, attaching `__source_file__` / `__row_index__`,
   multi-file `pl.concat`). Plugin authors don't need to import Polars
   at all unless they want the fast scan path.
4. (Optional) If your format declares column types (JSON, Parquet,
   Avro), declare `SOURCE_TYPE_ALIASES` to translate them to contract
   `Type` values. This unlocks the `field_types_from_sample` drift check.
   ```python
   SOURCE_TYPE_ALIASES = {
       "java.lang.String":  "string",
       "java.lang.Integer": "int32",
       # ...
   }
   ```
   Subclasses override `normalize_source_type(s)` for regex-style
   mapping (e.g. `"VARCHAR(N)"` → `"string"` for any N).
5. Add the module path to `_BUILTIN_MODULES` in
   [`data_parsers/__init__.py`](data_parsers/__init__.py).
6. Add a default block for your parser under `configs/parsers.yaml`:
   ```yaml
   my_parser:
     key1: default_value
     key2: ...
   ```
   Keys must match your `PARSER_PARAMS` allowlist (load-time typo gate).
7. Reference its `name` under `defaults.format:` or `tables.<T>.format:`
   in your `validation.yaml`.

### Config layering (last wins)

Parser params for one (format, table) merge through three layers:

1. `configs/parsers.yaml` `<format>:` block — per-format defaults.
2. `validation.yaml: defaults.parser_overrides:` — per-validation override.
3. `validation.yaml: tables.<T>.parser_overrides:` — per-table override.

The parser class itself carries no defaults — this is intentional, so
"what the parser does" (code) and "what values it uses" (YAML) never
disagree.

### Schema drift checks (opt-in)

When your `parse_file` exposes a `ParserSchema`, two opt-in structural
checks unlock automatically:

* `field_names_from_sample` — compare column names declared by the source
  against the contract's field names; warn on either-side drift.
* `field_types_from_sample` — compare source-declared column types (after
  `normalize_source_type` translation) against the contract types; warn
  on mismatch.

Both default off. Enable per-epic under `checks.structural:` in
`validation.yaml`. Both skip silently when the format doesn't carry the
relevant schema component (CSV runs never trigger `field_types_from_sample`
because CSV has no type info).

Examples: [`data_parsers/csv.py`](data_parsers/csv.py) (names only,
fast scan path), [`data_parsers/excel.py`](data_parsers/excel.py)
(names only, iterable path),
[`data_parsers/json_parser.py`](data_parsers/json_parser.py) (names +
types, iterable path, declares `SOURCE_TYPE_ALIASES`).

---

## After adding an extension

1. Restart any long-running process — registries populate at import time.
2. Re-run the validation; the YAML loader will require an explicit
   `true|false` for your new name in the matching tier.
3. Add a test under
   [`tests/field_constraints/`](../../tests/field_constraints/),
   [`tests/table_checks/`](../../tests/table_checks/),
   [`tests/metrics/`](../../tests/metrics/), or
   [`tests/data_parsers/`](../../tests/data_parsers/) following the
   registry-test pattern next to existing fixtures.

The CLI subcommands that consume your extension (`generate`,
`validate-contract`, `validate-data`) don't need any wiring — registry
dispatch handles everything.
