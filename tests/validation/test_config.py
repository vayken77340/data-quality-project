from __future__ import annotations

from pathlib import Path

import pytest

from data_contract.errors import ConfigError
from data_contract.validation.config import ValidationConfig
from tests.conftest import ALL_CHECKS_ENABLED_YAML, DEFAULT_TARGET_YAML


def _write(path: Path, text: str) -> Path:
    """Write `text` to `path`.

    For validation.yaml files, prepend the required `checks:` block AND the
    required `target:` field UNLESS the test already declared one (lets
    explicit-error tests still exercise the missing-block path).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == "validation.yaml":
        if "checks:" not in text:
            text = ALL_CHECKS_ENABLED_YAML + text
        if "target:" not in text:
            text = DEFAULT_TARGET_YAML + text
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_validation_yaml_loads(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  PROJECT:
    format: csv
    file_pattern: "project_*.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert "PROJECT" in cfg.tables
    t = cfg.tables["PROJECT"]
    assert t.format == "csv"
    assert t.file_pattern == "project_*.csv"
    assert t.field_mapping == {}
    assert t.parser_overrides == {}
    assert cfg.settings.extra_columns_severity == "warning"
    assert cfg.settings.rejected_row_cap == 500


def test_unknown_format_rejected(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: parquet  # not registered
    file_pattern: "x.parquet"
""")
    with pytest.raises(ConfigError, match="unknown parser"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_unknown_override_key_rejected(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    parser_overrides:
      bogus_key: value
""")
    with pytest.raises(ConfigError, match="unknown keys"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_field_mapping_must_be_string_to_string(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    field_mapping:
      "Spec Col": 123  # value isn't a string
""")
    with pytest.raises(ConfigError, match="field_mapping"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_invalid_settings_rejected(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
settings:
  extra_columns_severity: catastrophic
""")
    with pytest.raises(ConfigError, match="extra_columns_severity"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_effective_parser_params_uses_parser_yaml_defaults(tmp_path):
    """`configs/parsers.yaml` is the per-format defaults source. Top-level
    keys are format names; each block is the parser's default params."""
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "parsers.yaml", """
csv:
  delimiter: ";"
  encoding: utf-8
""")
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    params = cfg.effective_parser_params(cfg.tables["X"])
    assert params == {"delimiter": ";", "encoding": "utf-8"}


def test_effective_parser_params_layers_yaml_then_global_then_table(tmp_path):
    """Three-layer merge (last wins):
    1. configs/parsers.yaml[<format>:]      -- per-format defaults
    2. validation.yaml: defaults.parser_overrides: -- per-run global
    3. validation.yaml: tables.<T>.parser_overrides: -- per-table
    """
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "parsers.yaml", """
csv:
  delimiter: ","
  encoding: utf-8
""")
    _write(cfg_dir / "validation.yaml", """
defaults:
  parser_overrides:
    delimiter: ";"
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    parser_overrides:
      encoding: latin-1
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    params = cfg.effective_parser_params(cfg.tables["X"])
    # YAML base (delimiter "," + encoding utf-8), then defaults overrides
    # bump delimiter to ";", then table overrides bump encoding to latin-1.
    assert params == {"delimiter": ";", "encoding": "latin-1"}


def test_effective_parser_params_missing_yaml_returns_overrides_only(tmp_path):
    """Missing `configs/parsers.yaml` is OK -- the loader returns {} and
    the merge falls through to validation.yaml-side overrides."""
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    params = cfg.effective_parser_params(cfg.tables["X"])
    assert params == {}


def test_defaults_file_pattern_inherited_by_tables(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "data.xlsx"
tables:
  PROJECT:
  CALENDAR:
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.defaults.file_pattern == "data.xlsx"
    assert cfg.defaults.format == "excel"
    assert cfg.tables["PROJECT"].file_pattern == "data.xlsx"
    assert cfg.tables["PROJECT"].format == "excel"
    assert cfg.tables["CALENDAR"].file_pattern == "data.xlsx"


def test_per_table_format_and_pattern_override_defaults(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "fallback.xlsx"
tables:
  PROJECT:
    file_pattern: "project_special.xlsx"
  CALENDAR:
  ORDERS:
    format: csv
    file_pattern: "orders.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.tables["PROJECT"].format == "excel"
    assert cfg.tables["PROJECT"].file_pattern == "project_special.xlsx"
    assert cfg.tables["CALENDAR"].file_pattern == "fallback.xlsx"
    assert cfg.tables["ORDERS"].format == "csv"
    assert cfg.tables["ORDERS"].file_pattern == "orders.csv"


def test_missing_pattern_and_no_default_is_config_error(tmp_path):
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
tables:
  PROJECT:
  CALENDAR:
    file_pattern: "calendar.xlsx"
""")
    with pytest.raises(ConfigError, match="no file_pattern"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_missing_format_and_no_default_is_config_error(tmp_path):
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  file_pattern: "data.xlsx"
tables:
  PROJECT:
""")
    with pytest.raises(ConfigError, match="no format"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_empty_defaults_file_pattern_rejected(tmp_path):
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  file_pattern: ""
tables:
  PROJECT:
    format: excel
""")
    with pytest.raises(ConfigError, match="defaults.file_pattern"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_unknown_defaults_key_rejected(tmp_path):
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "data.xlsx"
  bogus_key: nope
tables:
  PROJECT:
""")
    with pytest.raises(ConfigError, match="unknown keys"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_tables_omitted_validates_all_discovered_contracts(tmp_path):
    """When `tables:` is absent, the discovered contracts are validated via defaults.

    Config-level only: this exercises is_filtered() + build_table_entry(); the runner
    integration is exercised in test_integration_1118.
    """
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "data.xlsx"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.is_filtered() is False
    entry = cfg.build_table_entry("AUTO_TABLE")
    assert entry.table == "AUTO_TABLE"
    assert entry.format == "excel"
    assert entry.file_pattern == "data.xlsx"


def test_tables_listed_is_filtered(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "data.xlsx"
tables:
  PROJECT:
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.is_filtered() is True
    assert list(cfg.tables) == ["PROJECT"]


def test_contracts_folder_parsed(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
contracts_folder: "somewhere/else/contracts"
defaults:
  format: excel
  file_pattern: "data.xlsx"
tables:
  PROJECT:
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.contracts_folder == Path("somewhere/else/contracts")


def test_contracts_folder_omitted_defaults_to_none(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: excel
  file_pattern: "data.xlsx"
tables:
  PROJECT:
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.contracts_folder is None


def test_defaults_parser_overrides_merge_with_table_overrides(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
defaults:
  format: csv
  file_pattern: "data.csv"
  parser_overrides:
    delimiter: ";"
    encoding: utf-8
tables:
  PROJECT:
    parser_overrides:
      delimiter: "|"      # override defaults
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    params = cfg.effective_parser_params(cfg.tables["PROJECT"])
    # delimiter from table wins, encoding from defaults survives
    assert params["delimiter"] == "|"
    assert params["encoding"] == "utf-8"


def test_sheet_name_under_parser_overrides_flows_into_effective_params(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  PROJECT:
    format: excel
    file_pattern: "data.xlsx"
    parser_overrides:
      sheet_name: "Project"
  CALENDAR:
    format: excel
    file_pattern: "data.xlsx"
    parser_overrides:
      sheet_name: "Calendar"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    assert cfg.effective_parser_params(cfg.tables["PROJECT"])["sheet_name"] == "Project"
    assert cfg.effective_parser_params(cfg.tables["CALENDAR"])["sheet_name"] == "Calendar"


def test_sheet_name_omitted_not_in_effective_params(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  PROJECT:
    format: excel
    file_pattern: "data.xlsx"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")
    params = cfg.effective_parser_params(cfg.tables["PROJECT"])
    assert "sheet_name" not in params


def test_sheet_name_under_parser_overrides_on_csv_rejected_at_load(tmp_path):
    """The PARSER_PARAMS allowlist gates `sheet_name` — declaring it on a CSV
    table raises ConfigError at YAML load (not later at parser instantiation)."""
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    parser_overrides:
      sheet_name: "Sheet1"
""")
    with pytest.raises(ConfigError, match="unknown keys"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_misplaced_top_level_sheet_name_rejected_at_load(tmp_path):
    """If a user puts `sheet_name` at the table top level (the old wrong spot),
    fail loud with a message that points them at parser_overrides."""
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: excel
    file_pattern: "x.xlsx"
    sheet_name: "Sheet1"
""")
    with pytest.raises(ConfigError, match="parser_overrides"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_misplaced_top_level_encoding_rejected_at_load(tmp_path):
    """Same guard for other format-specific keys."""
    import pytest
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    encoding: utf-8
""")
    with pytest.raises(ConfigError, match="parser_overrides"):
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


def test_violation_to_dict_round_trip():
    from data_contract.violations import Violation

    v = Violation(
        kind="nullable_violation",
        severity="error",
        table="PROJECT",
        field="proj_id",
        source_file="project_jan.csv",
        source_row=14,
        pk_values={"proj_id": 1234},
        offending_value=None,
        expected="value is required",
    )
    d = v.to_dict()
    assert d["kind"] == "nullable_violation"
    assert d["severity"] == "error"
    assert d["source_file"] == "project_jan.csv"
    assert d["source_row"] == 14
    assert d["pk_values"] == {"proj_id": 1234}


def test_violation_render_includes_field_and_location():
    from data_contract.violations import Violation

    v = Violation(
        kind="pattern_violation",
        severity="error",
        table="PROJECT",
        field="status",
        source_file="p.csv",
        source_row=27,
        offending_value="pendingg",
        expected="^[a-z]+$",
    )
    text = v.render()
    assert "status" in text
    assert "p.csv" in text
    assert "27" in text
    assert "pendingg" in text
