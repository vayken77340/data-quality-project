from __future__ import annotations

from pathlib import Path

import pytest

from data_contract.errors import ConfigError
from data_contract.validate_data.config import ValidationConfig


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")
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
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")


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
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")


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
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")


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
        ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")


def test_effective_parser_params_uses_parser_yaml_defaults(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "parsers" / "csv.yaml", """
delimiter: ";"
encoding: utf-8
""")
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")
    params = cfg.effective_parser_params(cfg.tables["X"])
    assert params == {"delimiter": ";", "encoding": "utf-8"}


def test_effective_parser_params_overrides_win(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "parsers" / "csv.yaml", """
delimiter: ","
encoding: utf-8
""")
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
    parser_overrides:
      delimiter: "|"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")
    params = cfg.effective_parser_params(cfg.tables["X"])
    # override delimiter wins; parser-yaml encoding survives
    assert params == {"delimiter": "|", "encoding": "utf-8"}


def test_effective_parser_params_missing_yaml_returns_overrides_only(tmp_path):
    cfg_dir = tmp_path / "configs"
    _write(cfg_dir / "validation.yaml", """
tables:
  X:
    format: csv
    file_pattern: "x.csv"
""")
    cfg = ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers")
    params = cfg.effective_parser_params(cfg.tables["X"])
    assert params == {}


def test_violation_to_dict_round_trip():
    from data_contract.validate_data.violations import Violation

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
    from data_contract.validate_data.violations import Violation

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
