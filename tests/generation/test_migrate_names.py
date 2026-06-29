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

CONTRACT_NO_SOURCE_NAME = """\
version: '1.0'
epic: 'x'
table: t
target: oracle

fields:
- name: a
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


def test_migrate_epic_renames_source_name(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {
        "t.yaml": CONTRACT_WITH_SOURCE_NAME,
        "history/1.0/t.yaml": CONTRACT_WITH_SOURCE_NAME,
    })

    report = migrate_epic("1118", tmp_path)

    assert report.errors == []
    assert report.files_changed == 2
    assert report.fields_renamed == 2
    for rel in ("t.yaml", "history/1.0/t.yaml"):
        text = (tmp_path / "1118" / "contracts" / rel).read_text(encoding="utf-8")
        assert "source_name" not in text
        assert "extract_name: A_raw" in text


def test_migrate_epic_is_idempotent(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    migrate_epic("1118", tmp_path)

    report = migrate_epic("1118", tmp_path)

    assert report.errors == []
    assert report.files_changed == 0
    assert report.fields_renamed == 0


def test_migrate_epic_unchanged_when_no_source_name(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_NO_SOURCE_NAME})

    report = migrate_epic("1118", tmp_path)

    assert report.files_changed == 0
    assert len(report.files_visited) == 1


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    original = (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8")

    report = migrate_epic("1118", tmp_path, dry_run=True)

    assert report.files_changed == 1
    assert report.fields_renamed == 1
    # File on disk unchanged.
    assert (tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8") == original


def test_extract_name_position_matches_old_source_name(tmp_path: Path) -> None:
    """Renaming source_name -> extract_name preserves the key's position
    (after `name`, before `type`). Loading the dumped YAML must yield the
    same key order."""
    _make_epic(tmp_path, "1118", {"t.yaml": CONTRACT_WITH_SOURCE_NAME})
    migrate_epic("1118", tmp_path)
    loaded = yaml.safe_load((tmp_path / "1118" / "contracts" / "t.yaml").read_text(encoding="utf-8"))
    first_field = loaded["fields"][0]
    assert list(first_field.keys()) == ["name", "extract_name", "type"]


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
