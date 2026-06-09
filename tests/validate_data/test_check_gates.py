"""Tests for `checks:` gates in validation.yaml.

Covers: CheckGates dataclass behaviour, YAML parsing (global + per-table +
merge), runner short-circuits per check.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from data_contract.cli import main
from data_contract.errors import ConfigError
from data_contract.validate_data.config import (
    CheckGates,
    CheckSpec,
    ValidationConfig,
    _parse_check_gates,
)


def _spec(enabled: bool) -> CheckSpec:
    return CheckSpec(enabled=enabled)
from tests.conftest import ALL_CHECKS_ENABLED_YAML


# ---------------------------------------------------------------------------
# CheckGates: defaults + override semantics
# ---------------------------------------------------------------------------


def test_check_gates_default_all_enabled():
    g = CheckGates()
    for name in ("type_coercion", "nullable", "pk_uniqueness", "fk_existence",
                 "min_value", "unique"):
        assert g.is_enabled(name) is True


def test_check_gates_explicit_disable():
    g = CheckGates(specs={"pk_uniqueness": _spec(False)})
    assert g.is_enabled("pk_uniqueness") is False
    # Other checks unaffected (defensive default).
    assert g.is_enabled("nullable") is True


def test_check_gates_with_overrides_merges_child_over_parent():
    parent = CheckGates(specs={
        "pk_uniqueness": _spec(False),
        "fk_existence": _spec(False),
    })
    # Child overrides pk_uniqueness; parent's fk_existence survives.
    child = CheckGates(specs={"pk_uniqueness": CheckSpec(enabled=True)})
    merged = parent.with_overrides(child)
    assert merged.is_enabled("pk_uniqueness") is True   # child wins
    assert merged.is_enabled("fk_existence") is False   # parent inherits
    assert merged.is_enabled("nullable") is True        # default


def test_check_gates_with_overrides_does_not_mutate():
    parent = CheckGates(specs={"pk_uniqueness": _spec(False)})
    child = CheckGates(specs={"fk_existence": _spec(False)})
    _ = parent.with_overrides(child)
    assert set(parent.specs) == {"pk_uniqueness"}
    assert set(child.specs) == {"fk_existence"}


# ---------------------------------------------------------------------------
# _parse_check_gates: typo gating, type gating
# ---------------------------------------------------------------------------


def test_parse_check_gates_empty():
    assert _parse_check_gates(None, ctx="x").specs == {}
    assert _parse_check_gates({}, ctx="x").specs == {}


def test_parse_check_gates_unknown_name_rejected():
    with pytest.raises(ConfigError, match="unknown check names"):
        _parse_check_gates({"pk_uniquenesss": False}, ctx="x")


def test_parse_check_gates_non_bool_enabled_rejected():
    with pytest.raises(ConfigError, match="must be a boolean"):
        _parse_check_gates({"pk_uniqueness": {"enabled": "false"}}, ctx="x")


def test_parse_check_gates_non_dict_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _parse_check_gates(["pk_uniqueness"], ctx="x")


def test_parse_check_gates_missing_enabled_rejected():
    with pytest.raises(ConfigError, match="missing required key 'enabled'"):
        _parse_check_gates({"pk_uniqueness": {}}, ctx="x")


def test_parse_check_gates_extra_key_rejected():
    with pytest.raises(ConfigError, match="unknown keys"):
        _parse_check_gates(
            {"pk_uniqueness": {"enabled": True, "description": "x"}},
            ctx="x",
        )


def test_parse_check_gates_shorthand_bool_accepted():
    """`name: true` is a valid shorthand for `{enabled: true}`."""
    g = _parse_check_gates({"pk_uniqueness": True, "fk_existence": False}, ctx="x")
    assert g.is_enabled("pk_uniqueness") is True
    assert g.is_enabled("fk_existence") is False


# ---------------------------------------------------------------------------
# ValidationConfig: global + per-table checks integration
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, content: str) -> Path:
    """Write validation.yaml fixture, auto-injecting `target: postgres` if the
    test didn't specify one. validation.yaml requires the target field."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dedent(content)
    if "target:" not in text:
        text = "target: postgres\n" + text
    path.write_text(text, encoding="utf-8")
    return path


def _full_checks_block(**overrides: bool) -> str:
    """Render a complete `checks:` block with optional per-key overrides.

    Defaults all to enabled=True. Used in tests that need to declare every
    check explicitly (the global block requires every check name)."""
    base = {
        "type_coercion":    True, "boolean_coercion": True,
        "nullable":         True, "max_length":       True,
        "column_missing":   True, "pk_uniqueness":    True,
        "fk_existence":     True, "allowed_values":   True,
        "pattern":          True, "min_value":        True,
        "max_value":        True, "format":           True,
        "unique":           True,
    }
    base.update(overrides)
    lines = ["checks:"]
    for name, v in base.items():
        lines.append(f"  {name}: {str(v).lower()}")
    return "\n".join(lines) + "\n"


def test_validation_yaml_global_checks_loaded(tmp_path: Path):
    p = _write_yaml(tmp_path / "validation.yaml",
        _full_checks_block(pk_uniqueness=False, fk_existence=False) +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "{table}.csv"
        """))
    cfg = ValidationConfig.from_yaml(p, tmp_path / "parsers")
    assert cfg.checks.is_enabled("pk_uniqueness") is False
    assert cfg.checks.is_enabled("fk_existence") is False
    assert cfg.checks.is_enabled("nullable") is True


def test_validation_yaml_per_table_overrides_global(tmp_path: Path):
    p = _write_yaml(tmp_path / "validation.yaml",
        _full_checks_block(pk_uniqueness=False) +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "{table}.csv"
            tables:
              PROJECT:
                checks:
                  pk_uniqueness: true       # re-enable for PROJECT
                  nullable: false           # disable nullable just for PROJECT
              CALENDAR:
        """))
    cfg = ValidationConfig.from_yaml(p, tmp_path / "parsers")
    project_checks = cfg.tables["PROJECT"].checks
    calendar_checks = cfg.tables["CALENDAR"].checks
    # PROJECT: pk_uniqueness re-enabled, nullable disabled
    assert project_checks.is_enabled("pk_uniqueness") is True
    assert project_checks.is_enabled("nullable") is False
    # CALENDAR: inherits global pk_uniqueness=false; defaults for everything else
    assert calendar_checks.is_enabled("pk_uniqueness") is False
    assert calendar_checks.is_enabled("nullable") is True


def test_validation_yaml_per_table_unknown_check_rejected(tmp_path: Path):
    p = _write_yaml(tmp_path / "validation.yaml",
        _full_checks_block() +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "{table}.csv"
            tables:
              PROJECT:
                checks:
                  nonexistent_check: false
        """))
    with pytest.raises(ConfigError, match="unknown check names"):
        ValidationConfig.from_yaml(p, tmp_path / "parsers")


def test_build_table_entry_inherits_global_checks(tmp_path: Path):
    """When `tables:` is omitted, discovered tables still inherit global checks."""
    p = _write_yaml(tmp_path / "validation.yaml",
        _full_checks_block(fk_existence=False) +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "{table}.csv"
        """))
    cfg = ValidationConfig.from_yaml(p, tmp_path / "parsers")
    entry = cfg.build_table_entry("ANY")
    assert entry.checks.is_enabled("fk_existence") is False
    assert entry.checks.is_enabled("nullable") is True


# ---------------------------------------------------------------------------
# Strictness: top-level `checks:` block is required, all keys must be present
# ---------------------------------------------------------------------------


def test_global_checks_block_required(tmp_path: Path):
    """A validation.yaml without a top-level `checks:` block is rejected."""
    p = _write_yaml(tmp_path / "validation.yaml", dedent("""\
        defaults:
          format: csv
          file_pattern: "{table}.csv"
    """))
    with pytest.raises(ConfigError, match="'checks' block is required"):
        ValidationConfig.from_yaml(p, tmp_path / "parsers")


def test_global_checks_block_must_list_every_check(tmp_path: Path):
    """A partial `checks:` block at the top level is rejected with a list of
    missing names (so the user knows exactly what to add)."""
    p = _write_yaml(tmp_path / "validation.yaml", dedent("""\
        checks:
          pk_uniqueness: false
        defaults:
          format: csv
          file_pattern: "{table}.csv"
    """))
    with pytest.raises(ConfigError, match="missing:.*nullable"):
        ValidationConfig.from_yaml(p, tmp_path / "parsers")


def test_per_table_checks_block_may_be_partial(tmp_path: Path):
    """The per-table block is an override; it only needs to list the keys
    that differ from global. Absent keys inherit."""
    p = _write_yaml(tmp_path / "validation.yaml",
        _full_checks_block() +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "{table}.csv"
            tables:
              PROJECT:
                checks:
                  pk_uniqueness: false      # only override; no other keys needed
        """))
    cfg = ValidationConfig.from_yaml(p, tmp_path / "parsers")
    project_checks = cfg.tables["PROJECT"].checks
    assert project_checks.is_enabled("pk_uniqueness") is False
    assert project_checks.is_enabled("nullable") is True       # inherited from global
    assert project_checks.is_enabled("fk_existence") is True   # inherited from global


# ---------------------------------------------------------------------------
# Runner integration: gates actually short-circuit checks
# ---------------------------------------------------------------------------


def _build_epic_with_dup_pk(tmp_path: Path) -> Path:
    """Mock epic with a PK uniqueness violation and an FK miss."""
    epic = tmp_path / "epics" / "TEST"
    (epic / "configs").mkdir(parents=True)
    (epic / "contracts").mkdir()
    (epic / "sample").mkdir()
    (tmp_path / "configs").mkdir()
    repo_root = Path(__file__).resolve().parents[2]
    (tmp_path / "configs" / "types.yaml").write_text(
        (repo_root / "configs" / "types.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # `target:` is required in validation.yaml -- copy target YAMLs into tmp.
    (tmp_path / "configs" / "targets").mkdir()
    for n in ("oracle.yaml", "postgres.yaml", "iceberg.yaml"):
        (tmp_path / "configs" / "targets" / n).write_text(
            (repo_root / "configs" / "targets" / n).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    parent = {
        "version": "1.0", "epic": "TEST", "table": "PARENT",
        "source": {"spec_file": "s", "spec_sheet": "PARENT"},
        "fields": [{"name": "pid", "type": "int64", "nullable": False, "primary_key": True}],
    }
    child = {
        "version": "1.0", "epic": "TEST", "table": "CHILD",
        "source": {"spec_file": "s", "spec_sheet": "CHILD"},
        "fields": [
            {"name": "cid", "type": "int64", "nullable": False, "primary_key": True},
            {"name": "pid", "type": "int64", "nullable": False,
             "foreign_key": {"table": "PARENT", "column": "pid"}},
        ],
    }
    (epic / "contracts" / "PARENT.yaml").write_text(
        yaml.safe_dump(parent, sort_keys=False), encoding="utf-8")
    (epic / "contracts" / "CHILD.yaml").write_text(
        yaml.safe_dump(child, sort_keys=False), encoding="utf-8")

    # PARENT: dup pid -> pk_not_unique
    (epic / "sample" / "PARENT.csv").write_text(
        "pid\n1\n1\n2\n", encoding="utf-8")
    # CHILD: pid=999 doesn't exist in PARENT -> fk_not_found
    (epic / "sample" / "CHILD.csv").write_text(
        "cid,pid\n10,1\n11,999\n", encoding="utf-8")
    return epic


def _run_with_checks(
    tmp_path: Path,
    *,
    global_overrides: dict | None = None,
    tables_yaml: str = "",
) -> dict:
    """Build an epic and run validate-data.

    `global_overrides`: per-key overrides for the global checks block
        (defaults are all-enabled; pass e.g. {"pk_uniqueness": False} to disable).
    `tables_yaml`: a raw YAML snippet starting with `tables:` for per-table
        overrides. Pass "" for none.
    """
    epic = _build_epic_with_dup_pk(tmp_path)
    base = (
        "target: postgres\n" +
        _full_checks_block(**(global_overrides or {})) +
        'defaults:\n'
        '  format: csv\n'
        '  file_pattern: "sample/{table}.csv"\n'
    )
    (epic / "configs" / "validation.yaml").write_text(
        base + dedent(tables_yaml), encoding="utf-8",
    )
    out = tmp_path / "out"
    rc = main([
        "validate-data", "--epic", "TEST",
        "--epic-root", str(tmp_path / "epics"),
        "--input-dir", str(epic),
        "--output-dir", str(out),
        "--types", str(tmp_path / "configs" / "types.yaml"),
    ])
    assert rc in (0, 2)
    return json.loads((out / "quality_report.json").read_text(encoding="utf-8"))


def _kinds(report: dict, table: str) -> set[str]:
    tbl = next(t for t in report["tables"] if t["table"] == table)
    return {v["kind"] for v in tbl["violations"]}


def test_default_gates_emit_pk_and_fk_violations(tmp_path: Path):
    """Baseline: every check enabled -- dup PK and dangling FK both surface."""
    report = _run_with_checks(tmp_path)
    assert "pk_not_unique" in _kinds(report, "PARENT")
    assert "fk_not_found" in _kinds(report, "CHILD")


def test_disable_pk_uniqueness_globally(tmp_path: Path):
    report = _run_with_checks(tmp_path, global_overrides={"pk_uniqueness": False})
    # PARENT no longer reports pk_not_unique.
    assert "pk_not_unique" not in _kinds(report, "PARENT")
    # FK still flagged.
    assert "fk_not_found" in _kinds(report, "CHILD")


def test_disable_fk_existence_globally(tmp_path: Path):
    report = _run_with_checks(tmp_path, global_overrides={"fk_existence": False})
    # CHILD no longer reports fk_not_found.
    assert "fk_not_found" not in _kinds(report, "CHILD")
    # PK still flagged on PARENT.
    assert "pk_not_unique" in _kinds(report, "PARENT")


def test_disable_pk_per_table(tmp_path: Path):
    """Disable PK uniqueness only for PARENT; other tables still enforce it."""
    report = _run_with_checks(tmp_path, tables_yaml="""\
        tables:
          PARENT:
            checks:
              pk_uniqueness: false
          CHILD:
    """)
    assert "pk_not_unique" not in _kinds(report, "PARENT")
    # FK on CHILD still runs (CHILD's gates inherit global defaults).
    assert "fk_not_found" in _kinds(report, "CHILD")


def test_disable_fk_per_table(tmp_path: Path):
    """Disable FK only for CHILD; PARENT's PK uniqueness untouched."""
    report = _run_with_checks(tmp_path, tables_yaml="""\
        tables:
          PARENT:
          CHILD:
            checks:
              fk_existence: false
    """)
    assert "pk_not_unique" in _kinds(report, "PARENT")
    assert "fk_not_found" not in _kinds(report, "CHILD")


def test_per_table_reenables_globally_disabled(tmp_path: Path):
    """Global disable + per-table re-enable -> only re-enabled tables run the check."""
    report = _run_with_checks(
        tmp_path,
        global_overrides={"pk_uniqueness": False},
        tables_yaml="""\
            tables:
              PARENT:
                checks:
                  pk_uniqueness: true     # re-enable for PARENT only
              CHILD:
        """,
    )
    # PARENT runs the check thanks to the per-table re-enable.
    assert "pk_not_unique" in _kinds(report, "PARENT")


def test_disable_nullable_check(tmp_path: Path):
    """Disable nullable check globally."""
    report = _run_with_checks(tmp_path, global_overrides={"nullable": False})
    assert "nullable_violation" not in _kinds(report, "PARENT")
    assert "nullable_violation" not in _kinds(report, "CHILD")
