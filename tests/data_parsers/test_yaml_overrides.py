"""Tests for `load_parser_yaml_overrides` -- the single-file YAML loader
that backs `configs/parsers.yaml`."""

from __future__ import annotations

from pathlib import Path

import pytest

from data_contract.data_parsers import load_parser_yaml_overrides
from data_contract.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"), encoding="utf-8")
    return path


def test_missing_file_returns_empty_dict(tmp_path):
    assert load_parser_yaml_overrides(tmp_path / "nope.yaml") == {}


def test_empty_file_returns_empty_dict(tmp_path):
    p = _write(tmp_path / "parsers.yaml", "")
    assert load_parser_yaml_overrides(p) == {}


def test_valid_file_round_trips_format_blocks(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
csv:
  encoding: utf-8
  delimiter: ","
  header_row: 1
excel:
  header_row: 2
""")
    out = load_parser_yaml_overrides(p)
    assert out == {
        "csv":   {"encoding": "utf-8", "delimiter": ",", "header_row": 1},
        "excel": {"header_row": 2},
    }


def test_empty_block_treated_as_no_overrides(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
csv:
""")
    assert load_parser_yaml_overrides(p) == {"csv": {}}


def test_top_level_non_mapping_rejected(tmp_path):
    p = _write(tmp_path / "parsers.yaml", "- not-a-mapping\n")
    with pytest.raises(ConfigError, match="top-level YAML must be a mapping"):
        load_parser_yaml_overrides(p)


def test_unknown_parser_name_rejected(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
parquet:
  compression: snappy
""")
    with pytest.raises(ConfigError, match="unknown parser format"):
        load_parser_yaml_overrides(p)


def test_block_non_mapping_rejected(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
csv: not-a-mapping
""")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_parser_yaml_overrides(p)


def test_unknown_key_inside_block_rejected(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
csv:
  bogus_key: 1
""")
    with pytest.raises(ConfigError, match="unknown keys"):
        load_parser_yaml_overrides(p)


def test_error_lists_accepted_keys_on_typo(tmp_path):
    p = _write(tmp_path / "parsers.yaml", """
csv:
  encodign: utf-8
""")
    # The error message surfaces the actual PARSER_PARAMS allowlist so
    # the typo is fixable from the diagnostic alone.
    with pytest.raises(ConfigError, match="accepted: "):
        load_parser_yaml_overrides(p)
