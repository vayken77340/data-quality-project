# data-contract

Repo for a spec-driven data-quality stack. Ships three Python packages
under `src/`:

- `data_contract/` -- spec (xlsx) -> contract (yaml) generator plus the
  file-side validator (`validate-data` on CSV/Excel/JSON).
- `dq_core/` -- shared substrate: contract model, plugin registries
  (field constraints, table checks, metrics), report writers (JSON,
  HTML, Markdown, XLSX). Imported by both peers; depends on neither.
- `warehouse_validation/` -- SQL-pushdown validator for warehouse
  tables. Four runners (bronze fidelity, silver conformance,
  reconciliation, gold assertions). Two connectors (Trino, Oracle).

Two console scripts: `data-contract` (generation + file validation)
and `warehouse-validation` (medallion validation).

## Layout

```
configs/
    types.yaml                  # global type registry (shared by all epics)
    specs_parsing.yaml          # global spec-column mapping (shared by all epics)
    targets/<target>.yaml       # per-target physical-type overlays (oracle, postgres, iceberg)
    parsers.yaml                # per-format parser defaults (csv, excel, json)
epics/<epic>/
    configs/
        contracts/<version>.yaml          # one file per version (e.g. v1.0.yaml, v2.0.yaml)
        validation.yaml                   # per-epic validate-data settings
        warehouse.yaml                    # per-epic bronze/silver physical-name mapping
    specs/<file>.xlsx           # the spec workbook
    contracts/
        <table>.yaml            # canonical contract for the highest version
        history/<v>/<table>.yaml          # versioned snapshot (every built version)
        history/<v>/joins.yaml            # joins snapshot for that version
        drift/<table>__v<from>_to_v<to>.yaml  # structured changelog -- written by `generate-drift`
        rejected/<table>.yaml             # only present when the spec for that table failed validation
    docs/
        data_dictionary.xlsx    # auto-generated per-epic XLSX (one sheet per table + README + Joins)
    rules/gold/<rule>.sql       # gold assertion SQL (one rule per file)
    rules/gold/<rule>.yaml      # sidecar metadata (name, table, severity, expected_count)
    validations/                # validate-data output (JSON, HTML, MD, XLSX)
    validations_warehouse/      # warehouse-validation output (subdirs: bronze/, reconcile/, gold/)
src/
    data_contract/              # generation + file validator + CLI
    dq_core/                    # shared contract model + registries + report writers
    warehouse_validation/       # four medallion runners + connectors + SQL pushdown
airflow_templates/
    medallion_dag.py            # example Airflow DAG; fork + adapt for your epic
```

## Installation

```bat
python -m venv .venv
.venv\Scripts\activate
```

Pick the extras you need:

```bat
pip install -e .                                  :: generation only
pip install -e .[validate-data]                   :: + file validator (polars, openpyxl, ...)
pip install -e .[validate-warehouse]              :: + Trino warehouse validator
pip install -e .[validate-warehouse-oracle]       :: + Oracle warehouse validator
pip install -e . -r requirements-dev.txt          :: tests + DuckDB (parity substrate)
```

The three warehouse extras are additive: install both
`[validate-warehouse]` and `[validate-warehouse-oracle]` if you need
both connectors at once. For an everything-installed dev setup:

```bat
pip install -e .[validate-data,validate-warehouse,validate-warehouse-oracle] -r requirements-dev.txt
```

## Quickstart

```bat
python -m data_contract generate --epic 1118
```

By default, `generate` builds **every** version under
`epics/<epic>/configs/contracts/`: the highest version becomes the
canonical contract (`contracts/<table>.yaml`), older versions are
written to `contracts/history/<v>/`. Pin a single version with
`--version 1.0` (older versions write only to `history/`; canonical is
untouched) or use `--config <path>`. Omit `--epic` to process every
epic under `epics/`.

Exit codes: `0` = all tables built clean, `2` = at least one
rejection, `1` = an epic couldn't be processed (bad config / missing
spec).

## Medallion architecture

The warehouse side validates a four-stage flow:

- **bronze** -- raw all-string ingest from source files. The bronze
  fidelity runner checks that every contract field is present as a
  column AND that every non-null string value is coercible to the
  contract's declared type via `TRY_CAST` (Trino) / equivalent
  (Oracle).
- **silver** -- typed, constraint-checked. The silver runner pushes
  every registered field constraint (`min_value`, `max_value`,
  `allowed_values`, `not_null`, `format`, `unique`, `pattern`) down
  as a SQL predicate and counts violating rows.
- **reconciliation** -- bronze vs silver row-count parity, with an
  `--expected-delta` knob for transformations that legitimately drop
  rows.
- **gold** -- per-rule SQL files under `epics/<E>/rules/gold/`. Each
  rule returns a scalar count; the runner compares it to the sidecar
  YAML's `expected_count`.

All four runners share the same exit-code contract (`0` pass, `1`
config error, `2` data violations) so they slot directly into an
Airflow `BashOperator` chain. See `airflow_templates/medallion_dag.py`
for a working example.

## Epic names

The epic name is also the folder name under `epics/` and the literal
`epic:` field stamped into every generated contract YAML, so the
rules are conservative on purpose:

- **Allowed**: ASCII letters, digits, spaces, and `.`, `_`, `-`. 1-64
  characters.
- **Must start AND end with a letter or digit** -- so `--foo`,
  `1118.`, ` 1118` are all rejected.
- **Reserved**: `.`, `..`, anything containing `..` (path-traversal
  guard), and the Windows-illegal characters `/ \ : * ? " < > |`.
- **No leading or trailing whitespace, no control characters, no
  Unicode letters** (Unicode normalisation across Windows / macOS /
  Linux causes surprises during folder iteration).

Valid examples: `1118`, `1118_MVP`, `1118 P1`, `customer_360`,
`acme-q3`.

Spaces are allowed but mean every CLI invocation needs quotes --
`python -m data_contract validate-data --epic "1118 MVP"`. If you
don't need spaces, prefer `1118_MVP` to avoid the paper-cut. The
validator runs at both the CLI argparse boundary and inside the
runner's library entry point, so library callers get the same
protection as CLI users.

## `data-contract` commands

- `generate` -- build contracts for every version under
  `configs/contracts/`. Highest version = canonical (`<table>.yaml`),
  older = history-only. `--version <x>` narrows to one version; if
  `x` isn't the highest, only `history/<x>/` is touched. Writes the
  data dictionary XLSX too.
- `lint` -- same validation pass as `generate` but writes
  **nothing**. Also gates `docs/constraints.md` freshness -- fails if
  a re-render would change the file. CI gate. Same exit codes
  (`0`/`2`/`1`).
- `generate-drift --epic E [--table T] [--from V1 --to V2] [--no-write]`
  -- generate drift files. Default: walks every consecutive history
  pair (`v_n-1 -> v_n`) for every table and writes one drift file per
  non-empty pair. `--from/--to` narrows to a specific pair; `--table`
  narrows to one table. `--no-write` is the dry-run mode.
- `export-schema [--out PATH]` -- render the contract JSON Schema
  (default `configs/schema.json`). Idempotent: only writes when
  content changes.
- `validate-contract [--epic E | --file PATH]` -- validate a contract
  YAML on disk against the JSON Schema and the semantic invariants
  (PK not nullable, max_length on strings only, FK targets exist,
  etc.). Different from `lint`: `lint` re-runs spec->contract,
  `validate-contract` checks a YAML file as-is. Exit codes same as
  `generate`.
- `validate-data --epic E [--table T]` -- load the contracts, glob
  the sample data per `validation.yaml.file_pattern`, run the
  configured checks and metrics, write the reports. All four formats
  (JSON, Markdown, HTML, XLSX) are written by default to
  `epics/<epic>/validations/`; `--json` adds a stdout echo of the
  JSON report on top of the file emission (it does NOT switch from
  "files" to "stdout").
- `regen-docs [--path docs/constraints.md]` -- rewrite the
  auto-generated catalog and format-token tables in
  `docs/constraints.md`. Run after adding/removing a
  `FieldConstraint` subclass or a format token; `lint` gates
  freshness (see above) and will fail in CI until this is re-run and
  the diff is committed.

## `warehouse-validation` commands

All four warehouse subcommands share the same flag block: `--epic`,
`--table` (optional on `validate-gold`), `--connector`,
`--epic-root` (default `epics/`), `--output-dir` (default
`epics/<epic>/validations_warehouse/`). Each writes reports in the
same four formats as `validate-data`.

- `validate-warehouse --epic E --table T --connector {trino,oracle}`
  -- validate the **silver** physical table against its contract via
  SQL pushdown. Issues `SELECT COUNT(*) WHERE <predicate>` per
  registered constraint; no row pulls.
- `validate-bronze --epic E --table T --connector {trino,oracle}` --
  **bronze fidelity**: every contract field is a column in bronze
  (case-sensitive) AND every non-null string is `TRY_CAST`-coercible
  to the contract's declared type. Reports land under
  `validations_warehouse/bronze/`.
- `validate-reconcile --epic E --table T --connector {trino,oracle}
  [--expected-delta N]` -- bronze vs silver `COUNT(*)` diff. Default
  expected delta is 0. Reports land under
  `validations_warehouse/reconcile/`.
- `validate-gold --epic E --connector {trino,oracle} [--table T]
  [--rule R]` -- execute every rule under `epics/<E>/rules/gold/`
  (or a subset). Each rule's `.sql` is run via
  `connector.execute_scalar` and compared to the sidecar's
  `expected_count`. Reports land under `validations_warehouse/gold/`.

Example -- run the full medallion sweep for one table:

```bat
warehouse-validation validate-bronze    --epic 1118 --table ipn_project --connector trino
warehouse-validation validate-warehouse --epic 1118 --table ipn_project --connector trino
warehouse-validation validate-reconcile --epic 1118 --table ipn_project --connector trino
warehouse-validation validate-gold      --epic 1118 --connector trino
```

Trino reads connection params from env: `TRINO_HOST`, `TRINO_USER`,
`TRINO_CATALOG`, optional `TRINO_PORT` (default 443) and
`TRINO_SCHEMA`. Oracle reads: `ORACLE_USER`, `ORACLE_PASSWORD`,
`ORACLE_DSN` (e.g. `host:port/service_name`), optional
`ORACLE_SCHEMA`.

`epics/<E>/configs/warehouse.yaml` maps logical table names to
fully-qualified bronze/silver coordinates. Missing file or missing
entry falls back to the bare-name convention `<table>_bronze` /
`<table>_silver` resolved by the connector's default catalog/schema.
The runner refuses to start if the resolved name still contains the
literal `placeholder_` substring -- update the file before pointing
the runner at a real warehouse.

## Airflow

`airflow_templates/medallion_dag.py` is a complete BashOperator-only
DAG: bronze >> silver >> reconcile >> gold. Each task invokes one
`warehouse-validation` subcommand; exit codes (`0`/`1`/`2`) are the
entire interface. Airflow's default trigger rules short-circuit
downstream tasks on non-zero exit.

To use:

1. Copy `airflow_templates/medallion_dag.py` into your Airflow DAGs
   folder.
2. Edit the four constants at the top: `EPIC`, `TABLE`, `CONNECTOR`,
   `EPIC_ROOT`. `EPIC_ROOT` should point at where your
   `epics/<epic>/` tree lives on the Airflow worker.
3. Confirm Airflow parses the DAG: `airflow dags list-import-errors`.

The template is documentation that happens to be a `.py` file --
`airflow` is not a runtime dependency of the repo. The repo CI only
syntax-checks the template via `python -m py_compile`.

## Field naming -- three layers

A contract field carries up to three names, one per medallion layer:

| Slot | What it names | Spec column | When emitted in YAML |
|---|---|---|---|
| `name`         | **silver** identifier (the typed DB column; the canonical reference for every consumer except the bronze runner and the file-side matcher) | derived `silver_raw`, then `slugify(bronze_raw)`, then `slugify(extract_raw)`; explicit `Nom BDD` override wins outright | always |
| `extract_name` | **raw extract** header as the spec author wrote it ("Reference Number", "Date d'envoi") | `Champ dans extract` | only when it differs from `name` |
| `bronze_name`  | **bronze** physical column when bronze diverges from silver (raw "Record Number" -> bronze "record no" -> silver "record_number") | optional `Nom Bronze` | only when it differs from `name` |

The silver `name` is derived by slugifying — but bronze wins over
extract because bronze is the same column in the same warehouse, just
with bad name hygiene, while extract may have semantic drift. Declare a
`Nom BDD` cell to override outright for acronyms, reserved words, or
team conventions the auto-slugifier can't infer; the generator uses
that verbatim and skips slugify for that row. Declare a `Nom Bronze`
cell when the bronze warehouse column name diverges from silver. A
bronze-only spec row (no extract, no silver) is legal — silver derives
from `slugify(bronze_raw)`.

At validation time the bronze runner cascades `bronze_name -> extract_name
-> name` when quoting SQL identifiers. So a contract with `extract_name`
set but no `bronze_name` probes bronze using the extract header; declare
`bronze_name` explicitly when the bronze warehouse uses silver-style
column names.

The validator's `exact` and `similarity` matching policies bind raw
CSV/Excel/JSON headers using **extract_name first, then silver
`name`**. Bronze is deliberately excluded from file-side matching --
bronze identifiers are warehouse-internal artifacts that don't appear
in upstream extract files. The per-table `field_mapping:` block in
`validation.yaml` was removed -- every header rename now lives on the
contract.

### Migrating pre-v2 contracts

> [!IMPORTANT]
> Contracts generated before this rename used `source_name:` for the
> raw extract header. Loading one through the current
> `Contract.from_dict` raises `ConfigError` -- there is no
> back-compatibility alias.
>
> Migrate per-epic contract YAMLs in place:
>
> ```
> python -m data_contract migrate-names --epic <E>
> ```
>
> Pass `--all` to walk every epic under `--epic-root`, `--path <DIR>`
> to migrate an arbitrary directory (e.g. `tests/fixtures/`), or
> `--dry-run` to preview. The tool rewrites `source_name:` ->
> `extract_name:` in every field block; it does not invent
> `bronze_name:` values. `epics/*/contracts/` is gitignored, so most
> operators only need to regenerate via `data_contract generate
> --epic <E>` -- the migration tool covers the case where on-disk
> artifacts can't be regenerated (test fixtures, snapshots).

## Target physical types

Each generated contract carries `target: <name>` (stamped from the
epic version config) and a per-field `physical_type` derived from the
target overlay (`configs/targets/<target>.yaml`). Spec readers see
what the database actually stores -- `BINARY_DOUBLE` for `float64` on
Oracle, `VARCHAR2(384 BYTE)` for `string(384)`, etc. The validator
never trusts the on-disk value; it recomputes from the active overlay
so a target change is honoured immediately. Spec authors never write
`physical_type` by hand.

## Field constraints

Beyond `name`/`type`/`description`/`nullable`, the contract carries
optional constraint fields driven by extra columns in the spec. Add a
column-mapping entry under `fields.column_mapping` in the epic's
`specs_parsing.yaml`:

| YAML key | Contract key | Cell type | spec_parsing knobs | contract_params knobs |
|---|---|---|---|---|
| `unique` | `unique` | bool | -- | -- |
| `allowed_values` | `allowed_values` | delimited string -> list | `separator` (default `\|`) | -- |
| `pattern` | `pattern` | raw regex | -- | -- |
| `min_value` | `min_value` | typed (int/float/iso-date/string-length) | -- | `strict` (default `false`, `>=` vs `>`) |
| `max_value` | `max_value` | typed | -- | `strict` (default `false`, `<=` vs `<`) |
| `format` | `format` | token from FORMAT_REGISTRY (`email`, `uuid`, `iban`, `phone`, ...) | -- | -- |
| `default_value` | `default` | typed against the field's declared type | `null_tokens` (list of placeholder strings treated as blank) | -- |

See [docs/constraints.md](docs/constraints.md) for the full catalog,
sub-block conventions, the structured-shape decision, and the
how-to-add walkthrough.

**Sub-blocks make the intent explicit:**
- `spec_parsing:` -- knobs the generator uses while parsing the spec
  cell. Never appear in the contract output.
- `contract_params:` -- knobs that flow into the contract output,
  sitting alongside the per-field value so the downstream validator
  knows how to check the data.

Constraints with `contract_params` emit a structured value
(`{value: ..., <params>}`); constraints without emit flat.

Example `specs_parsing.yaml`:

```yaml
fields:
  column_mapping:
    # Every column is value-required by default. Declare `default_value`
    # (any YAML value, including null) to make blanks tolerable; the
    # default is substituted in. `column_required: false` REQUIRES
    # `default_value` to be declared.
    extract_name: { spec_name: Field }
    silver_name:                   # optional override for the silver name; bypasses slugify
      spec_name: "Nom BDD"
      column_required: false
      default_value: null
    bronze_name:                   # optional; declare when bronze column name diverges from silver
      spec_name: "Nom Bronze"
      column_required: false
      default_value: null
    type:        { spec_name: Type }
    description:
      spec_name: Description
      column_required: false
      default_value: ""           # blank -> empty string
    nullable:
      spec_name: Obligatoire
      values:
        "true":  ["non"]
        "false": ["oui"]
    allowed_values:
      spec_name: "Allowed Values"
      column_required: false
      default_value: null         # blank -> omit the constraint
      spec_parsing:
        separator: ","          # generator-only: how to split the spec cell
    min_value:
      spec_name: "Min"
      column_required: false
      contract_params:
        strict: false           # emitted into contract: validator uses >=
    max_value:
      spec_name: "Max"
      column_required: false
      contract_params:
        strict: true            # emitted into contract: validator uses <
    pattern:
      spec_name: "Pattern"
      column_required: false
    unique:
      spec_name: "Unique?"
      column_required: false

keys:
  sheet_name: "..."
  column_mapping: { ... }

joins:
  sheet_name: "..."
  column_mapping: { ... }
```

All constraint columns are optional. A spec sheet that doesn't carry
a declared column simply produces fields without that key.

Contract output for a field that exercises the constraints above:

```yaml
- name: amount
  type: number
  min_value: { value: 0, strict: false }
  max_value: { value: 100, strict: true }
- name: status
  type: string
  allowed_values: [active, pending, archived]   # flat -- separator is spec-only
  pattern: "^[a-z]+$"
  unique: true
```

### Adding a custom constraint

Drop a new module under `src/dq_core/field_constraints/`:

```python
from dq_core.field_constraints.base import DriftChange, FieldConstraint

class StartsWithConstraint(FieldConstraint):
    name = "starts_with"
    contract_key = "starts_with"

    # Declare what YAML keys you accept under each sub-block. Unknown keys raise
    # ConfigError at parse time. Keys in CONTRACT_FIELDS are emitted into the
    # contract output via the base class's `to_contract_value`.
    SPEC_PARSING_FIELDS = ()
    CONTRACT_FIELDS = ()

    def _parse_non_empty(self, raw_str, raw_original, ctx):
        return raw_str, None

    @classmethod
    def diff(cls, field_name, old, new):
        if old == new:
            return None
        return DriftChange(kind="starts_with_changed", severity="breaking", field=field_name)
```

Add the module path to `_BUILTIN_MODULES` in
`dq_core/field_constraints/__init__.py` (auto-discovery picks up
`FieldConstraint` subclasses defined in those modules). Then
reference `starts_with` from any epic's `specs_parsing.yaml` under
`fields.column_mapping`.

If the constraint should also push down to SQL (warehouse-side), add
a sibling `predicate(field, check, dialect)` module under
`src/warehouse_validation/sql_predicates/<name>.py` and register it in
`sql_predicates/__init__.py`. The silver runner picks it up
automatically.

## Data dictionary

Each successful `generate` run also emits
`epics/<epic>/docs/data_dictionary.xlsx` -- an Excel workbook with:

- a **README** sheet (epic, version, source spec, counts),
- one sheet per generated table (field name, type, nullability,
  PK/FK, description, human-readable constraint summary),
- a **Joins** sheet when the epic has a joins contract.

Sheets have a frozen header row + auto-filter so the business
consumer can sort/filter natively the moment they open the workbook.
Skipped on `lint`.

## Adding a new epic

1. Create `epics/<epic>/{configs,specs,contracts}/`.
2. Drop the spec xlsx in `specs/`.
3. Add `configs/specs_parsing.yaml` describing how the spec's columns
   map onto `name`, `type`, `nullable`, `description`, optional
   `table`, plus any optional constraints. Constraint column mappings
   live under `fields.column_mapping`; the keys sheet under
   `keys.column_mapping`; joins under `joins.column_mapping`.
4. Add `configs/contracts/<anything>.yaml` with `epic`, `version`,
   `spec_file_name`, `tables`.
5. Run `python -m data_contract generate --epic <epic>`.
6. (Optional, file-side) Add `configs/validation.yaml` with the
   per-tier checks/metrics gates and the sample-data `file_pattern`.
7. (Optional, warehouse-side) Add `configs/warehouse.yaml` mapping
   each logical table to its bronze/silver fully-qualified physical
   name. Add gold rule pairs under `rules/gold/<rule>.sql` +
   `rules/gold/<rule>.yaml` (sidecar fields: `name`, `description`,
   `table`, optional `severity` defaulting to `error`, optional
   `expected_count` defaulting to `0`).

## Spec rejection

Per-table independent. Errors collected per table (not stop-on-first):

| Kind | Trigger |
|---|---|
| `missing_mandatory` | blank cell in a `mandatory: true` column |
| `unknown_type` | type not in the registry (silenceable via `--allow-unknown-types`) |
| `invalid_nullable` | nullable raw value not in the configured true/false set |
| `invalid_bool` | bool-constraint raw value not in the constraint's true/false set |
| `invalid_pattern` | regex doesn't compile |
| `list_empty` | list cell yields no values after splitting on the separator |
| `invalid_min_max` | min/max value isn't parseable as the field's type, or conflicts with max_length |
| `duplicate_field` | field name repeats within a table |
| `multi_table_in_sheet` | Table column has differing values within one sheet |
| `header_not_found` | required column header missing from the sheet |

A rejected table writes `contracts/rejected/<table>.yaml` and deletes
any stale `contracts/<table>.yaml`. The CLI exits with code 2 if any
rejection occurred.
