"""End-to-end tests for the bronze-silver reconciliation runner.

Uses the `warehouse_epic` fixture (synth contract, no warehouse.yaml),
so the runner resolves bare names `synth_bronze` / `synth_silver`.
FakeConnector's substring matcher distinguishes bronze vs silver by
inspecting the `FROM <name>` clause.
"""

from __future__ import annotations

import json
from pathlib import Path

from warehouse_validation.reconcile_runner import run_validate_reconcile


def _read_report(epic_root: Path) -> dict:
    path = (
        epic_root / "1118" / "validations_warehouse" / "reconcile"
        / "quality_report.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_match_default_delta_exits_zero(
    warehouse_epic, fake_connector_factory, capsys,
):
    fake_connector_factory(canned_counts={
        "FROM synth_bronze": 100,
        "FROM synth_silver": 100,
    })
    rc = run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(warehouse_epic)
    assert payload["run_issues"] == []
    out = capsys.readouterr().out
    assert "[VALIDATE-RECONCILE-OK]" in out
    assert "bronze=100 silver=100" in out


def test_mismatch_default_delta_exits_two(
    warehouse_epic, fake_connector_factory, capsys,
):
    fake_connector_factory(canned_counts={
        "FROM synth_bronze": 100,
        "FROM synth_silver": 95,
    })
    rc = run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = _read_report(warehouse_epic)
    mismatches = [v for v in payload["run_issues"]
                  if v["kind"] == "reconcile_row_count_mismatch"]
    assert len(mismatches) == 1
    v = mismatches[0]
    assert v["expected"] == "bronze - silver == 0"
    ov = v["offending_value"]
    assert "bronze=100" in ov
    assert "silver=95" in ov
    assert "actual_delta=5" in ov
    out = capsys.readouterr().out
    assert "[VALIDATE-RECONCILE-FAIL]" in out


def test_expected_delta_absorbs_known_drop(
    warehouse_epic, fake_connector_factory,
):
    fake_connector_factory(canned_counts={
        "FROM synth_bronze": 100,
        "FROM synth_silver": 95,
    })
    rc = run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
        expected_delta=5,
    )
    assert rc == 0
    payload = _read_report(warehouse_epic)
    assert payload["run_issues"] == []


def test_expected_delta_set_but_actual_differs_exits_two(
    warehouse_epic, fake_connector_factory,
):
    fake_connector_factory(canned_counts={
        "FROM synth_bronze": 100,
        "FROM synth_silver": 99,  # actual delta 1, but operator expected 5
    })
    rc = run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
        expected_delta=5,
    )
    assert rc == 2
    payload = _read_report(warehouse_epic)
    mismatches = [v for v in payload["run_issues"]
                  if v["kind"] == "reconcile_row_count_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["expected"] == "bronze - silver == 5"


def test_report_lands_in_reconcile_subdir(
    warehouse_epic, fake_connector_factory,
):
    fake_connector_factory(canned_counts={
        "FROM synth_bronze": 10,
        "FROM synth_silver": 10,
    })
    run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    p = (
        warehouse_epic / "1118" / "validations_warehouse" / "reconcile"
        / "quality_report.json"
    )
    assert p.is_file()


def test_yaml_mapped_names_used(
    warehouse_epic, fake_connector_factory,
):
    cfg = warehouse_epic / "1118" / "configs"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "warehouse.yaml").write_text(
        'tables:\n'
        '  synth:\n'
        '    bronze: "cat.sch.synth_bronze_v2"\n'
        '    silver: "cat.sch.synth_silver_v2"\n',
        encoding="utf-8",
    )
    fake = fake_connector_factory(canned_counts={
        "FROM cat.sch.synth_bronze_v2": 50,
        "FROM cat.sch.synth_silver_v2": 50,
    })
    rc = run_validate_reconcile(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    assert any("cat.sch.synth_bronze_v2" in q for q in fake.executed)
    assert any("cat.sch.synth_silver_v2" in q for q in fake.executed)


def test_missing_contract_returns_one(
    tmp_path, fake_connector_factory, capsys,
):
    fake_connector_factory()
    rc = run_validate_reconcile(
        epic="1118", table="ghost", connector_name="trino",
        epic_root=tmp_path, output_dir=None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "contract not found" in err
