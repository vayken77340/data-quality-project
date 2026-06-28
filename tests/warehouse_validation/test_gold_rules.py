"""Tests for the gold-rule discovery + sidecar parser."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from dq_core.errors import ConfigError
from warehouse_validation.gold_rules import (
    GoldRule,
    discover_rules,
    load_rule,
)


def _write_rule_pair(
    rules_dir: Path,
    name: str,
    sql: str,
    sidecar: dict[str, Any] | None,
) -> None:
    """Materialize a <name>.sql / <name>.yaml pair. If sidecar is None,
    skip the yaml file (orphan)."""
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / f"{name}.sql").write_text(sql, encoding="utf-8")
    if sidecar is not None:
        import yaml as _y
        (rules_dir / f"{name}.yaml").write_text(
            _y.safe_dump(sidecar, sort_keys=False),
            encoding="utf-8",
        )


def _default_sidecar(name: str, table: str = "t") -> dict[str, Any]:
    return {
        "name": name,
        "description": "desc",
        "table": table,
    }


# -- discover_rules: happy paths ----------------------------------------------


def test_discover_rules_happy_path_returns_sorted_list(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    _write_rule_pair(rules_dir, "z_rule", "SELECT 0", _default_sidecar("z_rule"))
    _write_rule_pair(rules_dir, "a_rule", "SELECT 0", _default_sidecar("a_rule"))
    rules = discover_rules(rules_dir)
    assert [r.name for r in rules] == ["a_rule", "z_rule"]
    assert all(isinstance(r, GoldRule) for r in rules)


def test_discover_rules_empty_dir_returns_empty_list(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    assert discover_rules(rules_dir) == []


def test_discover_rules_missing_dir_returns_empty_list(tmp_path):
    rules_dir = tmp_path / "absent"
    assert discover_rules(rules_dir) == []


def test_discover_rules_preserves_sql_verbatim(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sql = "SELECT COUNT(*) FROM t WHERE x < 0\n-- trailing comment\n"
    _write_rule_pair(rules_dir, "r", sql, _default_sidecar("r"))
    [rule] = discover_rules(rules_dir)
    assert rule.sql == sql


# -- discover_rules: orphans --------------------------------------------------


def test_orphan_sql_raises_config_error_naming_offender(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    (rules_dir / "lone.sql").write_text("SELECT 0", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "orphan" in msg
    assert "lone" in msg
    assert ".sql without matching .yaml" in msg


def test_orphan_yaml_raises_config_error_naming_offender(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    import yaml as _y
    (rules_dir / "lonely.yaml").write_text(
        _y.safe_dump(_default_sidecar("lonely")), encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "lonely" in msg
    assert ".yaml without matching .sql" in msg


def test_both_orphans_reported_together(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    (rules_dir / "alpha.sql").write_text("SELECT 0", encoding="utf-8")
    import yaml as _y
    (rules_dir / "beta.yaml").write_text(
        _y.safe_dump(_default_sidecar("beta")), encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "alpha" in msg
    assert "beta" in msg


# -- load_rule ----------------------------------------------------------------


def test_load_rule_happy_path(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    _write_rule_pair(rules_dir, "ok", "SELECT 0", _default_sidecar("ok", "tbl"))
    rule = load_rule(rules_dir, "ok")
    assert rule.name == "ok"
    assert rule.table == "tbl"


def test_load_rule_missing_name_raises_config_error(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    with pytest.raises(ConfigError) as exc:
        load_rule(rules_dir, "ghost")
    assert "ghost" in str(exc.value)


def test_load_rule_missing_only_sql_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    rules_dir.mkdir(parents=True)
    import yaml as _y
    (rules_dir / "x.yaml").write_text(
        _y.safe_dump(_default_sidecar("x")), encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_rule(rules_dir, "x")
    assert "x.sql" in str(exc.value)


# -- sidecar validation -------------------------------------------------------


def test_sidecar_name_mismatch_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("typo")
    _write_rule_pair(rules_dir, "actual", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "typo" in msg
    assert "actual" in msg


def test_sidecar_missing_description_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = {"name": "r", "table": "t"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "description" in str(exc.value)


def test_sidecar_missing_table_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = {"name": "r", "description": "d"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "table" in str(exc.value)


def test_sidecar_bogus_severity_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r") | {"severity": "critical"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "severity" in msg
    assert "error" in msg
    assert "warning" in msg


def test_sidecar_negative_expected_count_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r") | {"expected_count": -1}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "non-negative" in str(exc.value)


def test_sidecar_non_int_expected_count_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r") | {"expected_count": "five"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "integer" in str(exc.value)


def test_sidecar_unknown_top_level_key_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r") | {"dimension": "validity"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    msg = str(exc.value)
    assert "dimension" in msg
    assert "unknown top-level" in msg


def test_empty_sql_file_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    _write_rule_pair(rules_dir, "r", "   \n\n  ", _default_sidecar("r"))
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "empty" in str(exc.value)


def test_sidecar_default_severity_is_error(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r")  # no severity
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    [rule] = discover_rules(rules_dir)
    assert rule.severity == "error"


def test_sidecar_default_expected_count_is_zero(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = _default_sidecar("r")  # no expected_count
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    [rule] = discover_rules(rules_dir)
    assert rule.expected_count == 0


def test_sidecar_empty_description_raises(tmp_path):
    rules_dir = tmp_path / "rules" / "gold"
    sidecar = {"name": "r", "description": "   ", "table": "t"}
    _write_rule_pair(rules_dir, "r", "SELECT 0", sidecar)
    with pytest.raises(ConfigError) as exc:
        discover_rules(rules_dir)
    assert "description" in str(exc.value)
