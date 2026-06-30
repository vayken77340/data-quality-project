"""Happy / sad paths for the warehouse silver runner with a FakeConnector."""

from __future__ import annotations

import json

from warehouse_validation.runner import run_validate_warehouse


def test_clean_run_exits_zero_and_writes_four_reports(warehouse_epic, fake_connector_factory):
    fake = fake_connector_factory({})  # all queries return 0
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    out_dir = warehouse_epic / "1118" / "validations_warehouse"
    assert (out_dir / "quality_report.json").is_file()
    assert (out_dir / "quality_report.html").is_file()
    assert (out_dir / "quality_report.md").is_file()
    assert (out_dir / "quality_report.xlsx").is_file()
    # Three constraint pushdowns + one nullable check (pk is nullable: false).
    assert len(fake.executed) == 4
    assert any('"amount"' in q for q in fake.executed)
    assert any('"quota"' in q for q in fake.executed)
    assert any('"status"' in q for q in fake.executed)
    assert any('"pk" IS NULL' in q for q in fake.executed)


def test_violations_path_exits_two_and_reports_counts(warehouse_epic, fake_connector_factory):
    # Make min_value and allowed_values fail; max_value passes.
    fake_connector_factory({
        '"amount"': 7,
        '"status"': 3,
    })
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = json.loads(
        (warehouse_epic / "1118" / "validations_warehouse" / "quality_report.json")
        .read_text(encoding="utf-8")
    )
    # Warehouse violations carry no source_row so they land in run_issues
    # (the JSON writer's table-level catch-all), not in tables[].violations.
    kinds = {v["kind"]: v["offending_value"] for v in payload["run_issues"]}
    assert kinds["min_value_violation"] == 7
    assert kinds["allowed_values_violation"] == 3
    assert "max_value_violation" not in kinds


def test_unknown_epic_exits_one(tmp_path, fake_connector_factory, capsys):
    fake_connector_factory({})
    rc = run_validate_warehouse(
        epic="nonexistent_epic", table="synth", connector_name="trino",
        epic_root=tmp_path, output_dir=None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "validate-warehouse" in err
    assert "contract not found" in err


def test_pattern_constraint_dispatches_to_regexp_pushdown(tmp_path, fake_connector_factory):
    # Pattern now has a SQL pushdown -- the runner should issue one
    # regexp_like query (and nothing else, since no nullable: false).
    from tests.warehouse_validation.conftest import write_contract_yaml

    epic_root = tmp_path / "epics"
    write_contract_yaml(
        epic_root=epic_root, epic="1118", table="synth",
        field_blocks=[
            (
                '  - silver_name: code\n'
                '    type: string\n'
                '    nullable: true\n'
                '    pattern: "^[A-Z]{3}$"\n'
            ),
        ],
    )
    fake = fake_connector_factory({})
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=epic_root, output_dir=None,
    )
    assert rc == 0
    assert len(fake.executed) == 1
    assert "regexp_like" in fake.executed[0]
    assert '"code"' in fake.executed[0]


def test_output_dir_override_lands_at_absolute_path(warehouse_epic, fake_connector_factory, tmp_path):
    fake_connector_factory({})
    custom_out = tmp_path / "custom_reports"
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=custom_out,
    )
    assert rc == 0
    assert (custom_out / "quality_report.json").is_file()
    assert not (warehouse_epic / "1118" / "validations_warehouse" / "quality_report.json").exists()


def test_nullable_pass_emits_violation_on_nonzero_count(warehouse_epic, fake_connector_factory):
    # pk is nullable:false in the warehouse_epic fixture; canned count
    # of 5 on `"pk" IS NULL` should produce one nullable_violation.
    fake_connector_factory({'"pk" IS NULL': 5})
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 2
    payload = json.loads(
        (warehouse_epic / "1118" / "validations_warehouse" / "quality_report.json")
        .read_text(encoding="utf-8")
    )
    nullables = [
        v for v in payload["run_issues"]
        if v["kind"] == "nullable_violation"
    ]
    assert len(nullables) == 1
    v = nullables[0]
    assert v["field"] == "pk"
    assert v["offending_value"] == 5
    assert v["expected"] == "not null"


def test_unique_constraint_takes_count_query_branch(tmp_path, fake_connector_factory):
    # Build a contract whose only constraint is `unique` (count_query kind).
    # The runner should execute the predicate's SQL verbatim (no outer wrap).
    from tests.warehouse_validation.conftest import write_contract_yaml

    epic_root = tmp_path / "epics"
    write_contract_yaml(
        epic_root=epic_root, epic="1118", table="synth",
        field_blocks=[
            (
                '  - silver_name: invoice_id\n'
                '    type: string\n'
                '    nullable: true\n'
                '    unique: true\n'
            ),
        ],
    )
    # Canned: the inner subquery's HAVING clause is the marker.
    fake = fake_connector_factory({"HAVING COUNT(*) > 1": 3})
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=epic_root, output_dir=None,
    )
    assert rc == 2
    # The executed SQL starts with SELECT COUNT(*) FROM "synth" -- the
    # runner did NOT wrap it in another SELECT COUNT(*) FROM ... WHERE.
    assert len(fake.executed) == 1
    sql = fake.executed[0]
    assert sql.startswith('SELECT COUNT(*) FROM "synth"')
    # Critical: no double-wrapping.
    assert sql.count("SELECT COUNT(*)") == 1
    payload = json.loads(
        (epic_root / "1118" / "validations_warehouse" / "quality_report.json")
        .read_text(encoding="utf-8")
    )
    uniques = [v for v in payload["run_issues"] if v["kind"] == "unique_violation"]
    assert len(uniques) == 1
    assert uniques[0]["offending_value"] == 3
    assert uniques[0]["field"] == "invoice_id"


def test_nullable_pass_skips_nullable_true_fields(warehouse_epic, fake_connector_factory):
    # Make a query against `"amount" IS NULL` return nonzero just in
    # case; amount is nullable:true so the runner should NOT issue that
    # query.
    fake = fake_connector_factory({'"amount" IS NULL': 99})
    rc = run_validate_warehouse(
        epic="1118", table="synth", connector_name="trino",
        epic_root=warehouse_epic, output_dir=None,
    )
    assert rc == 0
    # Only nullable: false fields trigger an IS NULL scan.
    null_queries = [q for q in fake.executed if "IS NULL" in q]
    assert len(null_queries) == 1
    assert '"pk" IS NULL' in null_queries[0]
