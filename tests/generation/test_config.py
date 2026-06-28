from pathlib import Path

import pytest
import yaml

from data_contract.generation.config import (
    ALL_TABLES,
    Defaults,
    EpicConfig,
    merge,
    version_sort_key,
)
from dq_core.errors import ConfigError

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


def test_version_sort_key_orders_numeric_components_numerically():
    """1.10 must sort above 1.2 even though "1.10" < "1.2" lexically.
    This is the only edge case the lexical-vs-numeric distinction creates;
    pipeline._pick_canonical relies on it for canonical-version selection."""
    assert version_sort_key("1.10") > version_sort_key("1.2")
    assert version_sort_key("2.0") > version_sort_key("1.10")
    # Non-numeric components fall through to plain string ordering.
    assert version_sort_key("1.0-rc1") == ("1.0-rc1",)


def test_tables_all_sentinel(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "v.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntarget: postgres\ntables: all\n")
    cfg = EpicConfig.from_yaml(p)
    assert cfg.tables == ALL_TABLES


def test_tables_list(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(
        cfgs / "v.yaml",
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntarget: postgres\ntables:\n  - table_name: PROJECT\n",
    )
    cfg = EpicConfig.from_yaml(p)
    assert isinstance(cfg.tables, list)
    assert cfg.tables[0].table_name == "PROJECT"


def test_merge_uses_defaults_column_mapping(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    p = _write(cfgs / "v.yaml", "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntarget: postgres\ntables: all\n")
    defaults = Defaults.from_yaml(cfgs / "specs_parsing.yaml")
    cfg = EpicConfig.from_yaml(p)
    merged = merge(defaults, cfg)
    assert merged.column_mapping.name.spec_name == "Champ dans extract"


def test_merge_overrides_partial_column_mapping(tmp_path):
    cfgs = _make_epic_dir(tmp_path)
    override_yaml = (
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntarget: postgres\ntables: all\n"
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
    p = _write(cfgs / "v.yaml", "epic: X\nspec_file_name: f.xlsx\ntarget: postgres\ntables: all\n")
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
        "epic: X\nversion: '1.0'\nspec_file_name: f.xlsx\ntarget: postgres\ntables: all\n"
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
