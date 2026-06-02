# data-quality

Spec-driven data contract generator. Reads an Excel specification per epic and emits a YAML data contract per table, ready to drive downstream data-quality checks against sample files.

## Layout

```
configs/types.yaml              # global type registry (shared by all epics)
epics/<epic>/
    configs/
        defaults.yaml           # per-epic spec-column mapping (incl. optional constraints)
        <anything>.yaml         # one or more version configs (selected by `version` field)
    specs/<file>.xlsx           # the spec workbook
    contracts/
        <table>.yaml            # current contract, overwritten each successful run
        history/<table>/v<v>.yaml         # versioned snapshot
        drift/<table>__v<from>_to_v<to>.yaml  # structured changelog between two versions
        rejected/<table>.yaml             # only present when the spec for that table failed validation
```

## Quickstart

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev] -r requirements-dev.txt
python -m data_quality generate --epic 1118
```

The CLI auto-picks the highest-version config in `epics/<epic>/configs/`. Pin a specific one with `--version 1.0` or `--config <path>`. Omit `--epic` to process every epic under `epics/`.

Exit codes: `0` = all tables built clean, `2` = at least one rejection, `1` = an epic couldn't be processed (bad config / missing spec).

## Commands

- `generate` — build contracts. Writes `<table>.yaml`, history, drift, and rejected files as needed.
- `lint` — same validation pass as `generate` but writes **nothing**. CI gate. Same exit codes (`0`/`2`/`1`).
- `drift --epic E --table T --from 1.0 --to 2.0 [--write]` — compute drift between two existing history snapshots without regenerating. With `--write`, also emit the YAML to `contracts/drift/`.

## Field constraints

Beyond `name`/`type`/`description`/`nullable`, the contract carries optional flat constraint fields driven by extra columns in the spec. Add a column-mapping entry under `column_mapping` in the epic's `defaults.yaml`:

| YAML key | Contract field | Cell type | Extras |
|---|---|---|---|
| `primary_key` | `primary_key` | bool (OUI/NON-style) | optional `values:` block |
| `unique` | `unique` | bool | optional `values:` block |
| `list` | `list` | delimited string -> list | optional `separator:` (default `\|`) |
| `pattern` | `pattern` | raw regex | — |
| `min_value` | `min_value` | typed (int/float/iso-date/string-length) | — |
| `max_value` | `max_value` | typed | — |

Example:

```yaml
column_mapping:
  name: { spec_name: Field, mandatory: true }
  type: { spec_name: Type, mandatory: true }
  description: { spec_name: Description, mandatory: false }
  nullable:
    spec_name: Obligatoire
    mandatory: true
    values:
      "true":  ["non"]
      "false": ["oui"]
  primary_key: { spec_name: "PK?", mandatory: false }
  list:       { spec_name: "Allowed Values", mandatory: false, separator: "," }
  pattern:    { spec_name: "Pattern", mandatory: false }
```

All constraint columns are optional. A spec sheet that doesn't carry a declared column simply produces fields without that key.

### Adding a custom constraint

Drop a new module under `src/data_quality/field_constraints/`:

```python
from data_quality.field_constraints.base import (
    FieldConstraint, ConstraintContext, DriftChange, parse_column_ref,
)

class StartsWithConstraint(FieldConstraint):
    name = "starts_with"
    contract_key = "starts_with"

    @classmethod
    def from_config(cls, raw):
        c = cls(column=parse_column_ref(raw, name=cls.name))
        c.raw_config = dict(raw)
        return c

    def parse_cell(self, raw, ctx):
        if raw is None or str(raw).strip() == "":
            return None, None
        return str(raw).strip(), None

    @classmethod
    def diff(cls, field_name, old, new):
        if old == new:
            return None
        return DriftChange(kind="starts_with_changed", severity="breaking", field=field_name)
```

Add it to `_BUILTIN_MODULES` in `field_constraints/__init__.py` (auto-discovery picks up `FieldConstraint` subclasses defined in those modules). Then reference `starts_with` from any epic's `defaults.yaml` under `column_mapping`.

## History backfill

When you generate version N, any sibling configs with versions < N whose history snapshot is missing get auto-generated into `history/<table>/v<X>.yaml` — never into the canonical or rejected paths. Backfill is silent and idempotent:

- Existing `history/v<X>.yaml` files are never overwritten.
- If an older version's spec file is missing or would now reject, that version is skipped with a stderr warning and the target run continues.
- Disable with `--no-backfill`.

Caveat: if multiple versions share a `spec_file_name` and the spec was edited in place, backfill produces history from the *current* spec, not the historical one. Keep separate spec files per version if you need bit-perfect audit.

## Drift detection

After each successful build (target and backfilled versions), the CLI compares the new contract against the nearest-older history snapshot and writes a structured changelog to `contracts/drift/<table>__v<prev>_to_v<new>.yaml` if drift exists. The file is written only once per version pair — re-running the same target doesn't duplicate.

Severity classification:
- **breaking**: field removed, type changed, nullable tightened, max_length tightened, list added or values removed, pattern added, PK changed, unique added, min raised, max lowered.
- **additive**: field added (nullable), nullable relaxed, max_length relaxed, list values added, pattern removed, unique removed, min lowered, max raised.
- **cosmetic**: description changed.

No drift if there's nothing to compare against (single-version epic, first run).

## Adding a new epic

1. Create `epics/<epic>/{configs,specs,contracts}/`.
2. Drop the spec xlsx in `specs/`.
3. Add `configs/defaults.yaml` describing how the spec's columns map onto `name`, `type`, `nullable`, `description`, optional `table`, plus any optional constraints.
4. Add `configs/<anything>.yaml` with `epic`, `version`, `spec_file_name`, `tables`.
5. Run `python -m data_quality generate --epic <epic>`.

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
