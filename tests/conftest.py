from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.workbook.workbook import Workbook as WorkbookType

REPO_ROOT = Path(__file__).resolve().parents[1]
EPIC_1118 = REPO_ROOT / "epics" / "1118"
SAMPLE_SPEC = EPIC_1118 / "specs" / "Spec_example.xlsx"
TYPES_YAML = REPO_ROOT / "configs" / "types.yaml"


# Default keys-sheet column names used by helpers below + by the synthesized
# tmp epics in most tests. Keep these names ASCII so we don't have to wrestle
# with Excel encoding in the tests themselves.
KEYS_SHEET_NAME = "Keys"
KEYS_HEADERS = ("Table", "PK", "FK", "Comments")


# YAML snippet that explicitly enables every check. Used by `validate_data`
# tests when the test doesn't care about gating -- the top-level `checks:`
# block is required and tier-keyed, so this is the "I don't care, run
# everything" default.
ALL_CHECKS_ENABLED_YAML = """\
checks:
  structural:
    type_coercion: true
    boolean_coercion: true
    nullable: true
    max_length: true
  table:
    column_missing: true
    pk_uniqueness: true
    fk_existence: true
  field:
    allowed_values: true
    pattern: true
    min_value: true
    max_value: true
    format: true
    unique: true
"""

# YAML snippet that explicitly enables every metric. The top-level
# `metrics:` block is also required and tier-keyed by scope.
ALL_METRICS_ENABLED_YAML = """\
metrics:
  field:
    null_count: true
    null_percentage: true
    distinct_count: true
    completeness: true
    duplicate_pct: true
  table:
    row_count: true
"""

# Bundled "checks + metrics, everything enabled" -- tests that don't care
# about gating include this directly to satisfy the two required top-level
# blocks at once.
ALL_CHECKS_ENABLED_YAML = ALL_CHECKS_ENABLED_YAML + ALL_METRICS_ENABLED_YAML

# YAML snippet that satisfies the required `target:` field. Tests that don't
# care about target-specific behaviour use this. Postgres is chosen because
# its boolean tokens are the most permissive (accepts `true`/`false`/`t`/`f`
# /`yes`/`no`/`y`/`n`/`on`/`off`/`1`/`0`) so token-related test fixtures
# don't need to be tailored to a specific convention.
DEFAULT_TARGET_YAML = "target: postgres\n"


MINIMAL_DEFAULTS_YAML = """fields:
  column_mapping:
    name:
      spec_name: Champ dans extract
      value_required: true
    type:
      spec_name: Type
      value_required: true
    description:
      spec_name: Description
      column_required: false
    nullable:
      spec_name: Obligatoire
      value_required: true
      values:
        "true":  ["non", "no", "false", "0", "n"]
        "false": ["oui", "yes", "true", "1", "o", "y"]
"""


def minimal_defaults_yaml() -> str:
    """A complete defaults.yaml string with the standard column_mapping plus
    the minimal keys block (sheet `Keys`, ASCII headers, default separators)."""
    return MINIMAL_DEFAULTS_YAML + minimal_keys_block_yaml()


def minimal_keys_block_yaml(sheet_name: str = KEYS_SHEET_NAME) -> str:
    """The shortest valid `keys:` block. Used as a one-liner in test setup."""
    return (
        f"keys:\n"
        f"  sheet_name: {sheet_name!r}\n"
        f"  column_mapping:\n"
        f"    table_name:  {{ spec_name: '{KEYS_HEADERS[0]}', value_required: true }}\n"
        f"    primary_key: {{ spec_name: '{KEYS_HEADERS[1]}', value_required: true,  separator: '|' }}\n"
        f"    foreign_key: {{ spec_name: '{KEYS_HEADERS[2]}', separator: '|' }}\n"
        f"    comments:    {{ spec_name: '{KEYS_HEADERS[3]}', column_required: false }}\n"
    )


def add_keys_sheet(
    wb: WorkbookType,
    rows: list[tuple],
    *,
    sheet_name: str = KEYS_SHEET_NAME,
) -> None:
    """Append a Keys sheet to `wb` with the standard 4-column header.

    `rows` is a list of (table_name, pk_cell[, fk_cell[, comments_cell]]) tuples.
    Tuples of length 2-4 are accepted; missing elements default to None.
    """
    ws = wb.create_sheet(sheet_name)
    ws.append(list(KEYS_HEADERS))
    for row in rows:
        padded = list(row) + [None] * (4 - len(row))
        ws.append(padded[:4])


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def sample_spec_path() -> Path:
    return SAMPLE_SPEC


@pytest.fixture(scope="session")
def types_yaml_path() -> Path:
    return TYPES_YAML


@pytest.fixture()
def tiny_spec(tmp_path: Path) -> Path:
    """A tiny synthesized spec with: a leading non-spec sheet, then a spec sheet
    whose header sits on row 3 (so we exercise the search-depth path)."""
    path = tmp_path / "tiny.xlsx"
    wb = Workbook()

    sheet0 = wb.active
    sheet0.title = "ReadMe"
    sheet0.append(["This sheet isn't a table spec.", "Ignore me."])

    spec = wb.create_sheet("WIDGETS")
    spec.append(["preface", None, None, None])  # noise
    spec.append([None, None, None, None])       # blank
    spec.append(["Champ dans extract", "Type", "Description", "Obligatoire ?"])
    spec.append(["widget_id", "Double", "Identifier", "OUI"])
    spec.append(["label", "VARCHAR(50)", "Display label", "NON"])
    spec.append([None, None, None, None])  # trailing blank
    wb.save(path)
    return path
