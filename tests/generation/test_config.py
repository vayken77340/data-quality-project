from pathlib import Path

import pytest
import yaml

from data_contract.generation.config import (
    ALL_TABLES,
    Defaults,
    EpicConfig,
    merge,
    select_version_config,
)
from data_contract.errors import ConfigError

from tests.conftest import minimal_keys_block_yaml


DEFAULTS_YAML = """
fields:
  column_mapping:
    name:
      spec_name: Champ dans extract
    type:
      spec_name: Type
    description:
      spec_name: Description
      default_value: null
    nullable:
      spec_name: Obligatoire
      values:
        "true":  ["non"]
        "false": ["oui"]
""" + minimal_keys_block_yaml()


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _make_epic_dir(tmp_path: Path) -> Path:
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    _write(cfgs / "specs_parsing.yaml", DEFAULTS_YAML)
    return cfgs


def test_defaults_loads(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    defaults = Defaults.from_yaml(cfgs / "specs_parsing.yaml")
    assert defaults.column_mapping is not None
    assert defaults.column_mapping.name.spec_name == "Champ dans extract"
    # `name` has no `default_value` declared -> required.
    assert defaults.column_mapping.name.has_default is False
    # `description` declares `default_value` -> optional.
    assert defaults.column_mapping.description.has_default is True


def test_defaults_missing_file_returns_empty(tmp_path):
    defaults = Defaults.from_yaml(tmp_path / "missing.yaml")
    assert defaults.column_mapping is None


def test_select_highest_version_numeric(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    for v in ["1.0", "1.10", "2.0"]:
        _write(cfgs / f"cfg_{v}.yaml", f"epic: X\nversion: '{v}'\nspec_file_name: f.xlsx\ntables: all\n")
    chosen, reason = select_version_config(cfgs)
    assert chosen.version == "2.0"
    assert "highest" in reason


def test_select_highest_version_handles_1_10_as_greater_than_1_2(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    _write(cfgs / "a.yaml", "epic: X\nversion: '1.2'\nspec_file_name: f.xlsx\ntables: all\n")
    _write(cfgs / "b.yaml", "epic: X\nversion: '1.10'\nspec_file_name: f.xlsx\ntables: all\n")
    chosen, _ = select_version_config(cfgs)
    assert chosen.version == "1.10"


def test_select_explicit_version(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    _write(cfgs / "a.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n")
    _write(cfgs / "b.yaml", "epic: X\nversion: '2.0'\nspec_file_name: f.xlsx\ntables: all\n")
    chosen, reason = select_version_config(cfgs, version="1.0")
    assert chosen.version == "1.0"
    assert "1.0" in reason


def test_select_explicit_path_wins(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "weird.yaml", "epic: X\nversion: '7.7'\nspec_file_name: f.xlsx\ntables: all\n")
    chosen, reason = select_version_config(cfgs, explicit_path=p)
    assert chosen.version == "7.7"
    assert "explicit" in reason


def test_select_explicit_and_version_are_mutually_exclusive(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "a.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n")
    with pytest.raises(ConfigError):
        select_version_config(cfgs, version="1.0", explicit_path=p)


def test_select_empty_dir_raises(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    with pytest.raises(ConfigError):
        select_version_config(cfgs)


def test_defaults_yaml_is_never_picked_as_a_version(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    # Only specs_parsing.yaml exists in the dir; no version configs.
    with pytest.raises(ConfigError):
        select_version_config(cfgs)


def test_tables_all_sentinel(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "v.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n")
    cfg = EpicConfig.from_yaml(p)
    assert cfg.tables == ALL_TABLES


def test_tables_list(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(
        cfgs / "v.yaml",
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables:\n  - table_name: PROJECT\n",
    )
    cfg = EpicConfig.from_yaml(p)
    assert isinstance(cfg.tables, list)
    assert cfg.tables[0].table_name == "PROJECT"


def test_merge_uses_defaults_column_mapping(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "v.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n")
    defaults = Defaults.from_yaml(cfgs / "specs_parsing.yaml")
    cfg = EpicConfig.from_yaml(p)
    merged = merge(defaults, cfg)
    assert merged.column_mapping.name.spec_name == "Champ dans extract"


def test_merge_overrides_partial_column_mapping(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    override_yaml = (
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n"
        "fields:\n"
        "  column_mapping:\n"
        "    description:\n"
        "      spec_name: Field Description\n"
    )
    p = _write(cfgs / "v.yaml", override_yaml)
    defaults = Defaults.from_yaml(cfgs / "specs_parsing.yaml")
    cfg = EpicConfig.from_yaml(p)
    merged = merge(defaults, cfg)
    assert merged.column_mapping.description.spec_name == "Field Description"
    # other fields from defaults stay
    assert merged.column_mapping.name.spec_name == "Champ dans extract"


def test_missing_version_raises(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "v.yaml", "epic: X\nspec_file_name: f.xlsx\ntables: all\n")
    with pytest.raises(ConfigError):
        EpicConfig.from_yaml(p)


def test_merge_preserves_defaults_constraints_when_overriding_unrelated_field(tmp_path):
    """Regression test: an override of `description` must not drop the
    `unique` constraint declared in defaults."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    # Append the `unique` constraint block before the `keys:` block that lives
    # at the end of DEFAULTS_YAML. We re-build the defaults file from scratch
    # here so the constraint sits under `column_mapping:`.
    _write(cfgs / "specs_parsing.yaml", """
fields:
  column_mapping:
    name:        { spec_name: Champ dans extract }
    type:        { spec_name: Type }
    description: { spec_name: Description, default_value: null }
    nullable:
      spec_name: Obligatoire
      values:
        "true":  ["non"]
        "false": ["oui"]
    unique:
      spec_name: Unique
      default_value: null
""" + minimal_keys_block_yaml())
    override_yaml = (
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntables: all\n"
        "fields:\n"
        "  column_mapping:\n"
        "    description:\n"
        "      spec_name: Field Description\n"
    )
    p = _write(cfgs / "v.yaml", override_yaml)
    defaults = Defaults.from_yaml(cfgs / "specs_parsing.yaml")
    cfg = EpicConfig.from_yaml(p)
    merged = merge(defaults, cfg)
    assert "unique" in merged.column_mapping.constraints
    assert merged.column_mapping.description.spec_name == "Field Description"
