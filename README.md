# data-contract

Spec-driven data contract generator. Reads an Excel specification per epic and emits a YAML data contract per table, ready to drive downstream data-contract checks against sample files.

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
    specs/<file>.xlsx           # the spec workbook
    contracts/
        <table>.yaml            # canonical contract for the highest version
        history/<v>/<table>.yaml          # versioned snapshot (every built version)
        history/<v>/joins.yaml            # joins snapshot for that version
        drift/<table>__v<from>_to_v<to>.yaml  # structured changelog -- written by `generate-drift`
        rejected/<table>.yaml             # only present when the spec for that table failed validation
    docs/
        data_dictionary.xlsx    # auto-generated per-epic XLSX (one sheet per table + README + Joins)
```

## Quickstart

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev] -r requirements-dev.txt
python -m data_contract generate --epic 1118
```

By default, `generate` builds **every** version under `epics/<epic>/configs/contracts/`: the highest version becomes the canonical contract (`contracts/<table>.yaml`), older versions are written to `contracts/history/<v>/`. Pin a single version with `--version 1.0` (older versions write only to `history/`; canonical is untouched) or use `--config <path>`. Omit `--epic` to process every epic under `epics/`.

Exit codes: `0` = all tables built clean, `2` = at least one rejection, `1` = an epic couldn't be processed (bad config / missing spec).

## Epic names

The epic name is also the folder name under `epics/` and the literal `epic:` field stamped into every generated contract YAML, so the rules are conservative on purpose:

- **Allowed**: ASCII letters, digits, spaces, and `.`, `_`, `-`. 1–64 characters.
- **Must start AND end with a letter or digit** — so `--foo`, `1118.`, ` 1118` are all rejected.
- **Reserved**: `.`, `..`, anything containing `..` (path-traversal guard), and the Windows-illegal characters `/ \ : * ? " < > |`.
- **No leading or trailing whitespace, no control characters, no Unicode letters** (Unicode normalisation across Windows / macOS / Linux causes surprises during folder iteration).

Valid examples: `1118`, `1118_MVP`, `1118 P1`, `customer_360`, `acme-q3`.

Spaces are allowed but mean every CLI invocation needs quotes — `python -m data_contract validate-data --epic "1118 MVP"`. If you don't need spaces, prefer `1118_MVP` to avoid the paper-cut. The validator runs at both the CLI argparse boundary and inside the runner's library entry point, so library callers get the same protection as CLI users.

## Commands

- `generate` — build contracts for every version under `configs/contracts/`. Highest version = canonical (`<table>.yaml`), older = history-only. `--version <x>` narrows to one version; if `x` isn't the highest, only `history/<x>/` is touched. Writes the data dictionary XLSX too.
- `lint` — same validation pass as `generate` but writes **nothing**. Also gates `docs/constraints.md` freshness — fails if a re-render would change the file. CI gate. Same exit codes (`0`/`2`/`1`).
- `generate-drift --epic E [--table T] [--from V1 --to V2] [--no-write]` — generate drift files. Default: walks every consecutive history pair (`v_n-1 → v_n`) for every table and writes one drift file per non-empty pair. `--from/--to` narrows to a specific pair; `--table` narrows to one table. `--no-write` is the dry-run mode.
- `export-schema [--out PATH]` — render the contract JSON Schema (default `docs/contract-schema.json`). Idempotent: only writes when content changes.
- `validate-contract [--epic E | --file PATH]` — validate a contract YAML on disk against the JSON Schema and the semantic invariants (PK not nullable, max_length on strings only, FK targets exist, etc.). Different from `lint`: `lint` re-runs spec→contract, `validate-contract` checks a YAML file as-is. Exit codes same as `generate`.
- `validate-data --epic E [--table T]` — load the contracts, glob the sample data per `validation.yaml.file_pattern`, run the configured checks and metrics, write the JSON / Markdown / HTML / XLSX reports.
- `regen-docs [--path docs/constraints.md]` — rewrite the auto-generated catalog and format-token tables in `docs/constraints.md`.

## Field naming

The spec's `name` column carries the **business-friendly** header as written by spec authors ("Reference Number", "Date d'envoi"). The generator stores that verbatim as `source_name` on the contract and derives `name` -- the database identifier -- by slugifying it (lowercase, accents stripped, non-alphanumeric → `_`). When the slug already equals the source value, `source_name` is omitted from the YAML to keep already-clean rows quiet.

For acronyms, reserved words, or team conventions the auto-slugifier can't infer, declare a `Nom BDD` cell in the spec; the generator uses that as `name` verbatim and skips slugify for that row.

The validator's `exact` and `similarity` policies match incoming CSV/Excel/JSON headers against `source_name` first, then fall back to `name`. The per-table `field_mapping:` block in `validation.yaml` was removed -- every header rename now lives on the contract.

## Target physical types

Each generated contract carries `target: <name>` (stamped from the epic version config) and a per-field `physical_type` derived from the target overlay (`configs/targets/<target>.yaml`). Spec readers see what the database actually stores -- `BINARY_DOUBLE` for `float64` on Oracle, `VARCHAR2(384 BYTE)` for `string(384)`, etc. The validator never trusts the on-disk value; it recomputes from the active overlay so a target change is honoured immediately. Spec authors never write `physical_type` by hand.

## Field constraints

Beyond `name`/`type`/`description`/`nullable`, the contract carries optional constraint fields driven by extra columns in the spec. Add a column-mapping entry under `fields.column_mapping` in the epic's `defaults.yaml`:

| YAML key | Contract key | Cell type | spec_parsing knobs | contract_params knobs |
|---|---|---|---|---|
| `unique` | `unique` | bool | — | — |
| `allowed_values` | `allowed_values` | delimited string -> list | `separator` (default `\|`) | — |
| `pattern` | `pattern` | raw regex | — | — |
| `min_value` | `min_value` | typed (int/float/iso-date/string-length) | — | `strict` (default `false`, `>=` vs `>`) |
| `max_value` | `max_value` | typed | — | `strict` (default `false`, `<=` vs `<`) |
| `format` | `format` | token from FORMAT_REGISTRY (`email`, `uuid`, `iban`, `phone`, ...) | — | — |
| `default_value` | `default` | typed against the field's declared type | `null_tokens` (list of placeholder strings treated as blank) | — |

See [docs/constraints.md](docs/constraints.md) for the full catalog, sub-block conventions, the structured-shape decision, and the how-to-add walkthrough.

**Sub-blocks make the intent explicit:**
- `spec_parsing:` — knobs the generator uses while parsing the spec cell. Never appear in the contract output.
- `contract_params:` — knobs that flow into the contract output, sitting alongside the per-field value so the downstream validator knows how to check the data.

Constraints with `contract_params` emit a structured value (`{value: ..., <params>}`); constraints without emit flat.

Example `specs_parsing.yaml`:

```yaml
fields:
  column_mapping:
    # Every column is value-required by default. Declare `default_value`
    # (any YAML value, including null) to make blanks tolerable; the
    # default is substituted in. `column_required: false` REQUIRES
    # `default_value` to be declared.
    name:        { spec_name: Field }
    db_name:                       # optional override; bypasses slugify
      spec_name: "Nom BDD"
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

All constraint columns are optional. A spec sheet that doesn't carry a declared column simply produces fields without that key.

Contract output for a field that exercises the constraints above:

```yaml
- name: amount
  type: number
  min_value: { value: 0, strict: false }
  max_value: { value: 100, strict: true }
- name: status
  type: string
  allowed_values: [active, pending, archived]   # flat — separator is spec-only
  pattern: "^[a-z]+$"
  unique: true
```

### Adding a custom constraint

Drop a new module under `src/data_contract/field_constraints/`:

```python
from data_contract.field_constraints.base import DriftChange, FieldConstraint

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

Add the module path to `_BUILTIN_MODULES` in `field_constraints/__init__.py` (auto-discovery picks up `FieldConstraint` subclasses defined in those modules). Then reference `starts_with` from any epic's `defaults.yaml` under `fields.column_mapping`.

## History backfill

When you generate version N, any sibling configs with versions < N whose history snapshot is missing get auto-generated into `history/<X>/<table>.yaml` — never into the canonical or rejected paths. Backfill is silent and idempotent:

- Existing `history/<X>/<table>.yaml` files are never overwritten.
- If an older version's spec file is missing or would now reject, that version is skipped with a stderr warning and the target run continues.
- Disable with `--no-backfill`.

Each generated version also copies the source spec into `history/<X>/spec/<original_filename>`, so the historical truth is recoverable byte-for-byte even if the spec is later edited in place.

## Drift detection

After each successful build (target and backfilled versions), the CLI compares the new contract against the nearest-older history snapshot and writes a structured changelog to `contracts/drift/<table>__v<prev>_to_v<new>.yaml` if drift exists. The file is written only once per version pair — re-running the same target doesn't duplicate.

Severity classification:
- **breaking**: field removed, type changed, nullable tightened, max_length tightened, list added or values removed, pattern added, PK changed, unique added, min raised, max lowered.
- **additive**: field added (nullable), nullable relaxed, max_length relaxed, list values added, pattern removed, unique removed, min lowered, max raised.
- **cosmetic**: description changed.

No drift if there's nothing to compare against (single-version epic, first run).

## Data dictionary

Each successful `generate` run also emits `epics/<epic>/docs/data_dictionary.xlsx` — an Excel workbook with:

- a **README** sheet (epic, version, source spec, counts),
- one sheet per generated table (field name, type, nullability, PK/FK, description, human-readable constraint summary),
- a **Joins** sheet when the epic has a joins contract.

Sheets have a frozen header row + auto-filter so the business consumer can sort/filter natively the moment they open the workbook. Skipped on `lint`.

## Adding a new epic

1. Create `epics/<epic>/{configs,specs,contracts}/`.
2. Drop the spec xlsx in `specs/`.
3. Add `configs/defaults.yaml` describing how the spec's columns map onto `name`, `type`, `nullable`, `description`, optional `table`, plus any optional constraints. Constraint column mappings live under `fields.column_mapping`; the keys sheet under `keys.column_mapping`; joins under `joins.column_mapping`.
4. Add `configs/<anything>.yaml` with `epic`, `version`, `spec_file_name`, `tables`.
5. Run `python -m data_contract generate --epic <epic>`.

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

A rejected table writes `contracts/rejected/<table>.yaml` and deletes any stale `contracts/<table>.yaml`. The CLI exits with code 2 if any rejection occurred.
