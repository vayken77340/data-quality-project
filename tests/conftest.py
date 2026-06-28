from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.workbook.workbook import Workbook as WorkbookType

from dq_core.contract import Contract, FieldContract
from dq_core.type_mapping import Type


def field_contract(
    name: str,
    type_: Type = Type.STRING,
    *,
    nullable: bool = True,
    max_length: int | None = None,
    description: str | None = None,
) -> FieldContract:
    """Canonical FieldContract factory for tests.

    Default `nullable=True` matches FieldContract's natural shape for column
    checks / data validation tests. PK-enrichment tests in
    `tests/generation/test_keys.py` keep a local `_field()` wrapper with
    `nullable=False` so PK fields don't trip the `nullable_primary_key`
    invariant -- see that file for the rationale.
    """
    return FieldContract(
        name=name, type=type_, nullable=nullable,
        description=description, max_length=max_length,
    )


def contract(
    table: str = "T",
    *fields: FieldContract,
    epic: str = "E",
    version: str = "1.0",
    generated_at: str = "",
    spec_file: str = "",
    spec_sheet: str | None = None,
) -> Contract:
    """Canonical Contract factory for tests.

    `spec_sheet` defaults to `table` when omitted. Most tests don't care
    about epic/version/generated_at/spec_file -- the defaults match the
    "minimal valid Contract" shape most callers want. Override per-test
    when the assertion depends on a specific value.
    """
    return Contract(
        version=version, epic=epic, generated_at=generated_at,
        spec_file=spec_file, spec_sheet=spec_sheet if spec_sheet is not None else table,
        table=table, fields=list(fields),
    )

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
    field_names_from_sample: false
    field_types_from_sample: false
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

# Default target name for tests that need to stamp a contract or epic config.
# Postgres is chosen because its boolean tokens are the most permissive
# (accepts `true`/`false`/`t`/`f`/`yes`/`no`/`y`/`n`/`on`/`off`/`1`/`0`) so
# token-related test fixtures don't need to be tailored to a specific
# convention. `target:` no longer lives in validation.yaml -- it lives on
# the contract and on the epic version config; tests that need an end-to-end
# run stamp it via this constant.
DEFAULT_TARGET_NAME = "postgres"


# Hermetic parsers.yaml for integration tests that need a configs/parsers.yaml
# in their tmp tree. Mirrors what every test CSV fixture writes (comma
# delimiter, UTF-8, ", NULL" null tokens) rather than copying production --
# tests stay green even if the prod default changes.
TEST_PARSERS_YAML = """\
csv:
  encoding:              utf-8
  delimiter:             ","
  header_row:            1
  null_tokens:           ["", "NULL"]
  quote_char:            '"'
  field_matching_policy: positional

excel:
  header_row:            1
  null_tokens:           ["", "NULL"]
  field_matching_policy: positional

json:
  encoding:              utf-8
  header_path:           "data[0].report_header"
  rows_path:             "data[0].report_row"
  name_key:              "name"
  null_tokens:           ["", "null", "NULL"]
  field_matching_policy: exact
"""


def write_test_parsers_yaml(configs_dir: Path) -> None:
    """Write the hermetic parsers.yaml into `configs_dir`. Used by integration
    tests that build a tmp epic tree and need a parsers.yaml to satisfy the
    `<types-path>.parent / 'parsers.yaml'` lookup."""
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "parsers.yaml").write_text(TEST_PARSERS_YAML, encoding="utf-8")


MINIMAL_DEFAULTS_YAML = """fields:
  column_mapping:
    name:
      spec_name: Champ dans extract
    type:
      spec_name: Type
    description:
      spec_name: Description
      column_required: false
      default_value: null
    nullable:
      spec_name: Obligatoire
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
        f"    table_name:  {{ spec_name: '{KEYS_HEADERS[0]}' }}\n"
        f"    primary_key: {{ spec_name: '{KEYS_HEADERS[1]}',  separator: '|' }}\n"
        f"    foreign_key: {{ spec_name: '{KEYS_HEADERS[2]}', separator: '|', default_value: null }}\n"
        f"    comments:    {{ spec_name: '{KEYS_HEADERS[3]}', column_required: false, default_value: null }}\n"
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


def workbook_with_sheet(
    headers: tuple[str, ...] | list[str],
    rows: list[tuple],
    *,
    sheet_name: str,
    header_row: int = 1,
    placeholder_sheet_title: str = "Other",
) -> WorkbookType:
    """Build a Workbook with one named sheet carrying `headers` + `rows`.

    A placeholder default sheet (title `placeholder_sheet_title`) is created
    alongside so the spec-reader's sheet-search logic has noise to skip past.
    Each row is padded to `len(headers)`; rows longer than `headers` are
    truncated. `header_row > 1` pads with blank rows above the header.
    """
    wb = Workbook()
    wb.active.title = placeholder_sheet_title
    ws = wb.create_sheet(sheet_name)
    for _ in range(header_row - 1):
        ws.append([None] * len(headers))
    ws.append(list(headers))
    for row in rows:
        padded = list(row) + [None] * (len(headers) - len(row))
        ws.append(padded[: len(headers)])
    return wb


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
