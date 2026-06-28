"""CLI smoke tests for the validate-warehouse subcommand."""

from __future__ import annotations

import pytest

from warehouse_validation.cli import main


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["validate-warehouse", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "validate-warehouse" in out
    assert "--epic" in out
    assert "--table" in out
    assert "--connector" in out


def test_unknown_connector_rejected_by_argparse(capsys):
    # argparse choices=[...] rejects unknown values with exit code 2 before
    # the runner is even called -- standard argparse behaviour.
    with pytest.raises(SystemExit) as exc:
        main([
            "validate-warehouse",
            "--epic", "1118", "--table", "synth", "--connector", "snowflake",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "snowflake" in err
    # argparse's invalid-choice message lists every valid choice; both
    # supported connectors must appear.
    assert "trino" in err
    assert "oracle" in err


def test_missing_contract_returns_one(tmp_path, fake_connector_factory, capsys):
    fake_connector_factory({})
    rc = main([
        "validate-warehouse",
        "--epic", "1118", "--table", "ghost", "--connector", "trino",
        "--epic-root", str(tmp_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "contract not found" in err


def test_invalid_epic_name_rejected_by_argparse(capsys):
    with pytest.raises(SystemExit) as exc:
        main([
            "validate-warehouse",
            "--epic", "../escape", "--table", "synth", "--connector", "trino",
        ])
    assert exc.value.code == 2


def test_clean_run_via_cli_returns_zero(warehouse_epic, fake_connector_factory):
    fake_connector_factory({})
    rc = main([
        "validate-warehouse",
        "--epic", "1118", "--table", "synth", "--connector", "trino",
        "--epic-root", str(warehouse_epic),
    ])
    assert rc == 0


# -- validate-bronze ----------------------------------------------------------


def test_validate_bronze_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["validate-bronze", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "validate-bronze" in out
    assert "--epic" in out
    assert "--table" in out
    assert "--connector" in out


def test_validate_bronze_missing_contract_returns_one(
    tmp_path, fake_connector_factory, capsys,
):
    fake_connector_factory()
    rc = main([
        "validate-bronze",
        "--epic", "1118", "--table", "ghost", "--connector", "trino",
        "--epic-root", str(tmp_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "contract not found" in err


def test_validate_bronze_routes_to_bronze_runner(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "warehouse_validation.cli.run_validate_bronze", fake_run,
    )
    rc = main([
        "validate-bronze",
        "--epic", "1118", "--table", "synth", "--connector", "trino",
        "--epic-root", "epics",
    ])
    assert rc == 0
    assert captured["epic"] == "1118"
    assert captured["table"] == "synth"
    assert captured["connector_name"] == "trino"


# -- validate-reconcile -------------------------------------------------------


def test_validate_reconcile_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["validate-reconcile", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "validate-reconcile" in out
    assert "--expected-delta" in out


def test_validate_reconcile_expected_delta_parses_and_passes_through(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "warehouse_validation.cli.run_validate_reconcile", fake_run,
    )
    rc = main([
        "validate-reconcile",
        "--epic", "1118", "--table", "synth", "--connector", "trino",
        "--epic-root", "epics",
        "--expected-delta", "5",
    ])
    assert rc == 0
    assert captured["expected_delta"] == 5


def test_validate_reconcile_default_expected_delta_is_zero(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "warehouse_validation.cli.run_validate_reconcile", fake_run,
    )
    main([
        "validate-reconcile",
        "--epic", "1118", "--table", "synth", "--connector", "trino",
        "--epic-root", "epics",
    ])
    assert captured["expected_delta"] == 0


def test_validate_reconcile_missing_contract_returns_one(
    tmp_path, fake_connector_factory, capsys,
):
    fake_connector_factory()
    rc = main([
        "validate-reconcile",
        "--epic", "1118", "--table", "ghost", "--connector", "trino",
        "--epic-root", str(tmp_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "contract not found" in err


# -- validate-gold ------------------------------------------------------------


def test_validate_gold_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["validate-gold", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "validate-gold" in out
    assert "--epic" in out
    assert "--rule" in out
    assert "--connector" in out
    # --table is documented as optional (filter) on the gold subcommand.
    assert "--table" in out


def test_validate_gold_without_table_parses_and_routes(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "warehouse_validation.cli.run_validate_gold", fake_run,
    )
    rc = main([
        "validate-gold",
        "--epic", "1118", "--connector", "trino", "--epic-root", "epics",
    ])
    assert rc == 0
    assert captured["table"] is None
    assert captured["rule"] is None
    assert captured["epic"] == "1118"


def test_validate_gold_with_table_and_rule_filters(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "warehouse_validation.cli.run_validate_gold", fake_run,
    )
    main([
        "validate-gold",
        "--epic", "1118", "--connector", "trino", "--epic-root", "epics",
        "--table", "t1", "--rule", "my_rule",
    ])
    assert captured["table"] == "t1"
    assert captured["rule"] == "my_rule"


def test_validate_gold_unknown_rule_returns_one(
    tmp_path, fake_connector_factory, capsys,
):
    # No rules dir under tmp_path/1118; --rule asks for one anyway.
    (tmp_path / "1118" / "rules" / "gold").mkdir(parents=True)
    fake_connector_factory()
    rc = main([
        "validate-gold",
        "--epic", "1118", "--connector", "trino",
        "--epic-root", str(tmp_path),
        "--rule", "ghost",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost" in err
