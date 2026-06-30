from __future__ import annotations

from pathlib import Path

import yaml

from data_contract.migrate_names import (
    migrate_all,
    migrate_epic,
    migrate_path,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


CONTRACT_WITH_SOURCE_NAME = """\
version: '1.0'
epic: 'x'
table: t
target: oracle

fields:
- name: a
  source_name: A_raw
  type: string

- name: b
  type: int
"""

# Same shape as CONTRACT_WITH_SOURCE_NAME but with only the v2 `name:` key
# (no source_name). Exercises the v2->v3 rule in isolation.
CONTRACT_V2_NAME_ONLY = """\
version: '1.0'
epic: 'x'
table: t
target: oracle

fields:
- name: a
  type: string
"""

# Fully v3 + always-emit (all three name slots present). The tool must
# leave it untouched.
CONTRACT_ALREADY_V3 = """\
version: '1.0'
epic: 'x'
table: t
target: oracle

fields:
- silver_name: a
  extract_name: a
  bronze_name: a
  type: string
"""

CONTRACT_WITH_DATA_VALUES = """\
version: '1.0'
epic: 'x'
table: t
target: oracle

fields:
- name: flag
  source_name: Flag
  type: boolean
  data_values:
    'true': ['1', 'on', oui, t, 'true', vrai, y, 'yes']
    'false': ['0', f, 'false', faux, n, 'no', non, 'off']
"""


def _make_epic(root: Path, epic_id: str, files: dict[str, str]) -> None:
    contracts = root / epic_id / "contracts"
    for rel, text in files.items():
        _write(contracts / rel, text)


def test_migrate_epic_applies_both_rename_rules(tmp_path: Path) -> None:
    """v1->v2 (source_name -> extract_name) and v2->v3 (name -> silver_name)
    apply on the same pass. A field with both legacy keys gets both renamed;
    a field with only `name:` still gets renamed via v2->v3."""
    _make_epic(tmp_path, "1118", {
        "t.yaml": CONTRACT_WITH_SOURCE_NAME,
        "history/1.0/t.yaml": CONTRACT_WITH_SOURCE_NAME,
    })

    report = migrate_epic("1118", tmp_path)

    assert report.errors == []
    assert report.files_changed == 2
    # Two field blocks per file -- both need at least one rename -- across
    # two files -> 4.
    assert report.fields_renamed == 4
    for rel in ("t.yaml", "history/1.0/t.yaml"):
        loaded = yaml.safe_load(
            (tmp_path / "1118" / "contracts" / rel).read_text(encoding="utf-8")
        )
        # No legacy keys survive.
        for fb in loaded["fields"]:
            assert "source_name" not in fb
            assert "name" not in fb
        names = [fb["silver_name"] for fb in loaded["fields"]]
        assert names == ["a", "b"]
        # The source_name -> extract_name value is preserved verbatim.
        assert loaded["fields"][0]["extract_name"] == "A_raw"


def test_migrate_renames_name_to_silver_name(tmp_path: Path) -> None:
    """v2->v3 in isolation: a fixture with only `name:` (no source_name) gets
    renamed to silver_name."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_V2_NAME_ONLY})

    report = migrate_epic("1118", tmp_path)

    assert report.errors == []
    assert report.files_changed == 1
    assert report.fields_renamed == 1
    loaded = yaml.safe_load(
        (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")
    )
    # Materialization: extract_name and bronze_name default to silver_name's
    # value when absent from the input YAML.
    assert loaded["fields"][0] == {
        "silver_name": "a",
        "extract_name": "a",
        "bronze_name": "a",
        "type": "string",
    }


def test_migrate_materialises_missing_extract_and_bronze(tmp_path: Path) -> None:
    """Always-emit: a v2 field block missing extract_name and bronze_name
    gets both materialized to silver_name's value. Slot order matches what
    FieldContract.to_dict emits on a fresh generate."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_V2_NAME_ONLY})

    migrate_epic("1118", tmp_path)

    loaded = yaml.safe_load(
        (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")
    )
    # Insertion happens right after silver_name; check order.
    assert list(loaded["fields"][0].keys()) == [
        "silver_name", "extract_name", "bronze_name", "type",
    ]


def test_migrate_renames_both_source_name_and_name_on_same_field(tmp_path: Path) -> None:
    """Combined fixture: both renames fire on the same field on the same
    pass; materialization fills bronze_name. Canonical output order:
    silver_name -> extract_name -> bronze_name -> rest."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})

    migrate_epic("1118", tmp_path)

    loaded = yaml.safe_load(
        (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")
    )
    first_field = loaded["fields"][0]
    assert list(first_field.keys()) == [
        "silver_name", "extract_name", "bronze_name", "type",
    ]
    assert first_field["silver_name"]  == "a"
    assert first_field["extract_name"] == "A_raw"   # from source_name rename
    assert first_field["bronze_name"]  == "a"       # materialized to silver


def test_migrate_epic_is_idempotent(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    migrate_epic("1118", tmp_path)

    report = migrate_epic("1118", tmp_path)

    assert report.errors == []
    assert report.files_changed == 0
    assert report.fields_renamed == 0


def test_migrate_epic_unchanged_when_already_v3(tmp_path: Path) -> None:
    """A YAML already at v3 (silver_name + no legacy keys) is a no-op."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_ALREADY_V3})

    report = migrate_epic("1118", tmp_path)

    assert report.files_changed == 0
    assert len(report.files_visited) == 1


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    original = (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")

    report = migrate_epic("1118", tmp_path, dry_run=True)

    assert report.files_changed == 1
    # Two field blocks each need at least one rename.
    assert report.fields_renamed == 2
    # File on disk unchanged.
    assert (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8") == original


def test_data_values_stays_flow_style(tmp_path: Path) -> None:
    """data_values token lists must keep their inline ['1', 'on', ...] shape
    so the migration diff is just the key rename, not a style explosion."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_DATA_VALUES})

    migrate_epic("1118", tmp_path)

    text = (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")
    assert "'true': [" in text
    assert "'false': [" in text


def test_migrate_path_walks_arbitrary_directory(tmp_path: Path) -> None:
    """--path mode is used for the test fixture tree."""
    fixtures = tmp_path / "fixtures"
    _write(fixtures / "a.yaml", CONTRACT_WITH_SOURCE_NAME)
    _write(fixtures / "nested" / "b.yaml", CONTRACT_WITH_SOURCE_NAME)

    report = migrate_path(fixtures)

    assert report.errors == []
    assert report.files_changed == 2


def test_migrate_all_walks_every_epic(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    _make_epic(tmp_path, "1234", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})

    report = migrate_all(tmp_path)

    assert report.errors == []
    assert report.files_changed == 2


def test_invalid_yaml_aborts_without_writing(tmp_path: Path) -> None:
    """Plan: 'don't half-migrate'. One broken file aborts the whole run."""
    _make_epic(tmp_path, "1118", {"good.yaml": CONTRACT_WITH_SOURCE_NAME})
    bad = tmp_path / "1118" / "contracts" / "bad.yaml"
    bad.write_text("fields: [unclosed", encoding="utf-8")
    good_before = (tmp_path / "1118" / "contracts" / "good.yaml").read_text(encoding="utf-8")

    report = migrate_epic("1118", tmp_path)

    assert len(report.errors) == 1
    assert report.files_changed == 0
    # The good file must be untouched.
    assert (tmp_path / "1118" / "contracts" / "good.yaml").read_text(encoding="utf-8") == good_before


def test_non_mapping_top_level_is_an_error(tmp_path: Path) -> None:
    contracts = tmp_path / "1118" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "scalar.yaml").write_text("just a string\n", encoding="utf-8")

    report = migrate_epic("1118", tmp_path)

    assert len(report.errors) == 1


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    report = migrate_epic("does-not-exist", tmp_path)
    assert len(report.errors) == 1
    assert report.files_changed == 0
