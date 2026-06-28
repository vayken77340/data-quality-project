"""End-to-end tests for the gold assertions runner.

Reuses `fake_connector_factory` from conftest. Adds a small
`_write_gold_rules` helper that materializes
`epics/<E>/rules/gold/<name>.sql` + `.yaml` pairs.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml as _y

from warehouse_validation.gold_runner import run_validate_gold


def _write_gold_rules(
    epic_root: Path,
    epic: str,
    rules: list[dict[str, Any]],
) -> Path:
    """Write a list of {name, sql, sidecar} dicts under
    epic_root/<epic>/rules/gold/. Returns the rules directory path."""
    rules_dir = epic_root / epic / "rules" / "gold"
    rules_dir.mkdir(parents=True, exist_ok=True)
    for r in rules:
        (rules_dir / f"{r['name']}.sql").write_text(r["sql"], encoding="utf-8")
        (rules_dir / f"{r['name']}.yaml").write_text(
            _y.safe_dump(r["sidecar"], sort_keys=False), encoding="utf-8",
        )
    return rules_dir


def _write_contract(
    epic_root: Path,
    epic: str,
    table: str,
) -> Path:
    """Drop a minimal one-field contract YAML so the gold runner can
    optionally resolve contract_version/pk_fields. Mirrors the
    `write_contract_yaml` helper in conftest but inlined for the gold
    tests' specific shape."""
    contracts_dir = epic_root / epic / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""\
        version: "9.9"
        epic: "{epic}"
        generated_at: "2026-06-28T00:00:00Z"
        spec:
          file_path: "tests/synthetic.xlsx"
          sheet_name: "synthetic"
        table: {table}
        target: postgres
        fields:
          - name: pk
            type: int64
            nullable: false
            primary_key: true
        """)
    path = contracts_dir / f"{table}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _read_report(epic_root: Path, epic: str = "1118") -> dict:
    path = (
        epic_root / epic / "validations_warehouse" / "gold"
        / "quality_report.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def gold_epic(tmp_path):
    return tmp_path / "epics"


# -- happy path ---------------------------------------------------------------


def test_happy_path_no_violations_exits_zero(
    gold_epic, fake_connector_factory, capsys,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r1",
            "sql": "SELECT 0 FROM t1_silver",
            "sidecar": {"name": "r1", "description": "d1", "table": "t1"},
        },
        {
            "name": "r2",
            "sql": "SELECT 0 FROM t1_silver",
            "sidecar": {"name": "r2", "description": "d2", "table": "t1"},
        },
    ])
    fake_connector_factory(canned_counts={})  # default returns 0
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(gold_epic)
    assert payload["run_issues"] == []
    # exactly one TableReport (one table covered by both rules)
    assert len(payload["tables"]) == 1
    out = capsys.readouterr().out
    assert "[VALIDATE-GOLD-OK]" in out
    assert "2 passed, 0 failed across 1 table" in out


# -- error-severity violation -------------------------------------------------


def test_failed_rule_error_severity_exits_two(
    gold_epic, fake_connector_factory, capsys,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "amount_nonneg",
            "sql": "SELECT 5 -- pretend 5 bad rows",
            "sidecar": {"name": "amount_nonneg", "description": "d",
                        "table": "t1"},
        },
    ])
    fake_connector_factory(canned_counts={"pretend 5 bad rows": 5})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 2
    payload = _read_report(gold_epic)
    violations = [
        v for v in payload["run_issues"]
        if v["kind"] == "gold_rule_violation"
    ]
    assert len(violations) == 1
    v = violations[0]
    assert v["severity"] == "error"
    assert v["expected"] == "amount_nonneg: count == 0"
    assert v["offending_value"] == "actual_count=5"
    assert v["table"] == "t1"
    out = capsys.readouterr().out
    assert "[VALIDATE-GOLD-FAIL]" in out


# -- warning severity does NOT trip exit 2 ------------------------------------


def test_warning_severity_violation_does_not_trip_exit_two(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "soft_check",
            "sql": "SELECT 1 -- soft fail",
            "sidecar": {"name": "soft_check", "description": "d",
                        "table": "t1", "severity": "warning"},
        },
    ])
    fake_connector_factory(canned_counts={"soft fail": 1})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(gold_epic)
    warnings = [
        v for v in payload["run_issues"]
        if v["severity"] == "warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "gold_rule_violation"


# -- --table filter -----------------------------------------------------------


def test_table_filter_runs_only_matching_rules(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "rt1",
            "sql": "SELECT 0 FROM t1_silver -- t1 marker",
            "sidecar": {"name": "rt1", "description": "d", "table": "t1"},
        },
        {
            "name": "rt2",
            "sql": "SELECT 0 FROM t2_silver -- t2 marker",
            "sidecar": {"name": "rt2", "description": "d", "table": "t2"},
        },
    ])
    fake = fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table="t1", rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    # Only the t1 rule's SQL was executed.
    assert any("t1 marker" in q for q in fake.executed)
    assert not any("t2 marker" in q for q in fake.executed)


# -- --rule filter ------------------------------------------------------------


def test_rule_filter_runs_only_named_rule(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r1", "sql": "SELECT 0 -- r1 marker",
            "sidecar": {"name": "r1", "description": "d", "table": "t1"},
        },
        {
            "name": "r2", "sql": "SELECT 0 -- r2 marker",
            "sidecar": {"name": "r2", "description": "d", "table": "t1"},
        },
    ])
    fake = fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule="r1", connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    assert any("r1 marker" in q for q in fake.executed)
    assert not any("r2 marker" in q for q in fake.executed)


def test_rule_filter_missing_name_returns_one(
    gold_epic, fake_connector_factory, capsys,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r1", "sql": "SELECT 0",
            "sidecar": {"name": "r1", "description": "d", "table": "t1"},
        },
    ])
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule="absent", connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "absent" in err


# -- sidecar contract resolution ---------------------------------------------


def test_table_without_contract_uses_none_metadata(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r", "sql": "SELECT 0",
            "sidecar": {"name": "r", "description": "d", "table": "ghost"},
        },
    ])
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(gold_epic)
    tr = payload["tables"][0]
    assert tr["contract_version"] == "(none)"
    assert tr["pk_fields"] == []


def test_table_with_contract_uses_contract_metadata(
    gold_epic, fake_connector_factory,
):
    _write_contract(gold_epic, "1118", "t_with_contract")
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r", "sql": "SELECT 0",
            "sidecar": {"name": "r", "description": "d",
                        "table": "t_with_contract"},
        },
    ])
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(gold_epic)
    tr = payload["tables"][0]
    assert tr["contract_version"] == "9.9"
    assert tr["pk_fields"] == ["pk"]


# -- multiple tables ---------------------------------------------------------


def test_multiple_tables_produce_multiple_table_reports(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r1", "sql": "SELECT 0",
            "sidecar": {"name": "r1", "description": "d", "table": "t1"},
        },
        {
            "name": "r2", "sql": "SELECT 0",
            "sidecar": {"name": "r2", "description": "d", "table": "t2"},
        },
    ])
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(gold_epic)
    table_names = sorted(t["table"] for t in payload["tables"])
    assert table_names == ["t1", "t2"]


# -- connector returns NULL --------------------------------------------------


def test_connector_returns_null_returns_one(
    gold_epic, fake_connector_factory, capsys,
):
    # Override execute_scalar to return None.
    from warehouse_validation.connectors.base import Connector

    class _NullConnector(Connector):
        def execute_scalar(self, sql):
            return None
        def execute_count(self, sql):
            return 0
        def execute_columns(self, n):
            return set()

    import warehouse_validation.setup as s
    s.get_connector = lambda name: _NullConnector()

    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r", "sql": "SELECT NULL",
            "sidecar": {"name": "r", "description": "d", "table": "t1"},
        },
    ])
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "r" in err
    assert "NULL" in err


# -- empty rules dir ---------------------------------------------------------


def test_empty_rules_dir_exits_zero(
    gold_epic, fake_connector_factory, capsys,
):
    (gold_epic / "1118" / "rules" / "gold").mkdir(parents=True)
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 passed, 0 failed across 0 table(s)" in out


def test_missing_rules_dir_exits_zero(
    gold_epic, fake_connector_factory,
):
    # epic dir exists but rules/gold/ does not
    (gold_epic / "1118").mkdir(parents=True)
    fake_connector_factory(canned_counts={})
    rc = run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    assert rc == 0


# -- report subdir -----------------------------------------------------------


def test_report_lands_in_gold_subdir(
    gold_epic, fake_connector_factory,
):
    _write_gold_rules(gold_epic, "1118", [
        {
            "name": "r", "sql": "SELECT 0",
            "sidecar": {"name": "r", "description": "d", "table": "t1"},
        },
    ])
    fake_connector_factory(canned_counts={})
    run_validate_gold(
        epic="1118", table=None, rule=None, connector_name="trino",
        epic_root=gold_epic, output_dir=None,
    )
    p = (
        gold_epic / "1118" / "validations_warehouse" / "gold"
        / "quality_report.json"
    )
    assert p.is_file()
