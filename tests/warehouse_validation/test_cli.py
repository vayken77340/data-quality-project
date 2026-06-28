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
            "--epic", "1118", "--table", "synth", "--connector", "oracle",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "oracle" in err
    assert "trino" in err


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
