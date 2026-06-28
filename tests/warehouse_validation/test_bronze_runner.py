"""End-to-end tests for the bronze fidelity runner.

Uses the `warehouse_epic` fixture from conftest -- it ships a `synth`
contract whose four fields (`pk:int64`, `amount:int64`, `quota:int64`,
`status:string`) cover both the TRY_CAST path (3 ints) and the
STRING-skip path (1 string).
"""

from __future__ import annotations

import json
from pathlib import Path

from warehouse_validation.bronze_runner import run_validate_bronze


# Bare-name fallback: no warehouse.yaml exists in the tmp epic root, so
# load_mapping returns "synth_bronze" / "synth_silver".
BRONZE_NAME = "synth_bronze"


def _bronze_with_all_contract_columns() -> set[str]:
    return {"pk", "amount", "quota", "status"}


def _read_report(epic_root: Path, subdir: str = "bronze") -> dict:
    path = (
        epic_root / "1118" / "validations_warehouse" / subdir
        / "quality_report.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_happy_path_no_violations_exits_zero(
    warehouse_epic, fake_connector_factory, capsys,
):
    fake = fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: _bronze_with_all_contract_columns()},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    payload = _read_report(warehouse_epic)
    assert payload["run_issues"] == []
    # 3 TRY_CAST queries fired (one per non-string field), no scan of `status`.
    try_cast_queries = [q for q in fake.executed if "TRY_CAST" in q]
    assert len(try_cast_queries) == 3
    assert all('TRY_CAST("status"' not in q for q in try_cast_queries)
    out = capsys.readouterr().out
    assert "[VALIDATE-BRONZE-OK]" in out


def test_missing_column_emits_one_violation_exits_two(
    warehouse_epic, fake_connector_factory,
):
    # Bronze missing `amount` -> one bronze_missing_column violation.
    cols = _bronze_with_all_contract_columns() - {"amount"}
    fake = fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: cols},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = _read_report(warehouse_epic)
    violations = payload["run_issues"]
    missing = [v for v in violations if v["kind"] == "bronze_missing_column"]
    assert len(missing) == 1
    assert missing[0]["field"] == "amount"
    # The TRY_CAST scan for `amount` MUST be skipped (the column doesn't exist).
    assert not any('TRY_CAST("amount"' in q for q in fake.executed)


def test_extra_column_emits_one_violation_exits_two(
    warehouse_epic, fake_connector_factory,
):
    cols = _bronze_with_all_contract_columns() | {"bonus"}
    fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: cols},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = _read_report(warehouse_epic)
    violations = payload["run_issues"]
    extras = [v for v in violations if v["kind"] == "bronze_extra_column"]
    assert len(extras) == 1
    assert extras[0]["field"] == "bonus"


def test_uncoercible_emits_one_violation_with_count(
    warehouse_epic, fake_connector_factory,
):
    # TRY_CAST("amount" AS BIGINT) IS NULL for 7 rows.
    fake_connector_factory(
        canned_counts={'TRY_CAST("amount" AS BIGINT)': 7},
        columns_by_table={BRONZE_NAME: _bronze_with_all_contract_columns()},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = _read_report(warehouse_epic)
    violations = payload["run_issues"]
    uncoercible = [v for v in violations if v["kind"] == "bronze_uncoercible"]
    assert len(uncoercible) == 1
    v = uncoercible[0]
    assert v["field"] == "amount"
    assert v["offending_value"] == 7
    assert "coercible to int64" in v["expected"]


def test_string_fields_skip_try_cast(
    warehouse_epic, fake_connector_factory,
):
    fake = fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: _bronze_with_all_contract_columns()},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    # `status` is the only string field; ensure no TRY_CAST query was issued for it.
    assert not any('TRY_CAST("status"' in q for q in fake.executed)


def test_report_lands_in_bronze_subdir(
    warehouse_epic, fake_connector_factory,
):
    fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: _bronze_with_all_contract_columns()},
    )
    run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    expected = (
        warehouse_epic / "1118" / "validations_warehouse" / "bronze"
        / "quality_report.json"
    )
    assert expected.is_file()


def test_columns_query_targets_bronze_table(
    warehouse_epic, fake_connector_factory,
):
    fake = fake_connector_factory(
        canned_counts={},
        columns_by_table={BRONZE_NAME: _bronze_with_all_contract_columns()},
    )
    run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert fake.column_lookups == [BRONZE_NAME]


def test_yaml_mapped_bronze_name_is_used(
    warehouse_epic, fake_connector_factory,
):
    # Drop a warehouse.yaml so the bronze name becomes the FQ entry from yaml.
    cfg = warehouse_epic / "1118" / "configs"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "warehouse.yaml").write_text(
        'tables:\n'
        '  synth:\n'
        '    bronze: "cat.sch.synth_bronze_v2"\n'
        '    silver: "cat.sch.synth_silver_v2"\n',
        encoding="utf-8",
    )
    fake = fake_connector_factory(
        canned_counts={},
        columns_by_table={"cat.sch.synth_bronze_v2": _bronze_with_all_contract_columns()},
    )
    rc = run_validate_bronze(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    assert fake.column_lookups == ["cat.sch.synth_bronze_v2"]
    # And the TRY_CAST queries target the FQ name too.
    assert all("cat.sch.synth_bronze_v2" in q for q in fake.executed if "TRY_CAST" in q)


def test_missing_contract_returns_one(
    tmp_path, fake_connector_factory, capsys,
):
    fake_connector_factory()
    rc = run_validate_bronze(
        epic="1118", table="ghost", connector_name="trino",
        epic_root=tmp_path, output_dir=None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "contract not found" in err
