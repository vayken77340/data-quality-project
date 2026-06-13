from __future__ import annotations

from pathlib import Path

import yaml

from data_contract.cli import main


def _write_contract(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _minimal_payload(table: str = "T", **field_overrides) -> dict:
    field = {"name": "x", "type": "int64", "nullable": False}
    field.update(field_overrides)
    return {
        "version": "1.0",
        "epic": "E",
        "generated_at": "2026-06-04T10:00:00Z",
        "source": {"spec_file": "epics/E/specs/spec.xlsx", "spec_sheet": table},
        "table": table,
        "fields": [field],
    }


def _bootstrap_validate_layout(tmp_path: Path, repo_root: Path) -> Path:
    """Mirror the global type registry under tmp_path so the CLI can find it."""
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "types.yaml").write_text(
        (repo_root / "configs" / "types.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def test_validate_contract_clean_single_file(tmp_path, repo_root, monkeypatch):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload())
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 0


def test_validate_contract_detects_nullable_pk(tmp_path, repo_root, monkeypatch):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload(primary_key=True, nullable=True))
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2


def test_validate_contract_detects_max_length_on_integer(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload(max_length=50))
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2
    out = capsys.readouterr()
    assert "max_length_only_on_string" in out.err


def test_validate_contract_detects_precision_on_integer(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload(precision=10))
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2
    assert "precision_scale_only_on_decimal" in capsys.readouterr().err


def test_validate_contract_detects_scale_gt_precision(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    payload = _minimal_payload(type="float64")
    payload["fields"][0]["precision"] = 5
    payload["fields"][0]["scale"] = 10
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2
    assert "precision_scale_consistency" in capsys.readouterr().err


def test_validate_contract_detects_duplicate_field_names(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    payload = _minimal_payload()
    payload["fields"].append({"name": "x", "type": "string", "nullable": True})
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2
    assert "duplicate_field_names" in capsys.readouterr().err


def test_validate_contract_rejects_unknown_constraint_key(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    payload = _minimal_payload()
    # Schema's additionalProperties: false will catch this first.
    payload["fields"][0]["unknown_constraint"] = 42
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2


def test_validate_contract_legacy_flat_min_value_rejected(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    payload = _minimal_payload(type="float64")
    payload["fields"][0]["min_value"] = 5  # flat — should fail the structured-shape requirement
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2


def test_validate_contract_cross_table_fk_target_exists(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    contracts = tmp_path / "epics" / "E" / "contracts"

    pk = _minimal_payload(table="PARENT", name="parent_id", primary_key=True)
    pk["fields"][0]["name"] = "parent_id"
    _write_contract(contracts / "PARENT.yaml", pk)

    child = _minimal_payload(table="CHILD")
    child["fields"][0]["name"] = "parent_id"
    child["fields"][0]["foreign_key"] = {"table": "PARENT", "column": "MISSING"}
    _write_contract(contracts / "CHILD.yaml", child)

    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "fk_target_exists" in capsys.readouterr().err


def test_validate_contract_cross_table_fk_clean(tmp_path, repo_root, monkeypatch):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    contracts = tmp_path / "epics" / "E" / "contracts"

    pk = _minimal_payload(table="PARENT")
    pk["fields"][0] = {"name": "parent_id", "type": "int64", "nullable": False, "primary_key": True}
    _write_contract(contracts / "PARENT.yaml", pk)

    child = _minimal_payload(table="CHILD")
    child["fields"][0] = {
        "name": "parent_id", "type": "int64", "nullable": False,
        "foreign_key": {"table": "PARENT", "column": "parent_id"},
    }
    _write_contract(contracts / "CHILD.yaml", child)

    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 0


def test_validate_contract_against_real_1118_clean(tmp_path, repo_root, monkeypatch):
    """The actual repo's epic 1118 contracts must validate cleanly."""
    monkeypatch.chdir(repo_root)
    rc = main(["validate-contract", "--epic", "1118"])
    assert rc == 0


# ---------------------------------------------------------------------------
# P0.1 — generate self-check
# ---------------------------------------------------------------------------


def test_generate_self_check_against_real_1118_clean(repo_root, monkeypatch):
    """Generate against the real epic 1118 — the self-check should pass."""
    monkeypatch.chdir(repo_root)
    rc = main(["generate", "--epic", "1118"])
    assert rc == 0


def test_generate_self_check_catches_dangling_fk(tmp_path, repo_root, monkeypatch, capsys):
    """Sanity-check: confirm the self-check on a healthy epic exits 0; the
    negative path is exercised by direct unit tests on check_invariants.

    With the in-code default `allow_foreign_key_violation=true`, a dangling
    FK is demoted to a warning by the keys layer before self-check runs, so
    we can't reach a surviving fk_target_exists invariant violation through
    the CLI without staging an in-memory contract directly."""
    from openpyxl import Workbook
    from tests.conftest import minimal_defaults_yaml, add_keys_sheet
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)

    edir = tmp_path / "epics" / "E"
    (edir / "configs").mkdir(parents=True)
    (edir / "specs").mkdir(parents=True)
    (edir / "contracts").mkdir(parents=True)


    (tmp_path / "configs" / "default_spec_configs.yaml").write_text(minimal_defaults_yaml(), encoding="utf-8")
    (edir / "configs" / "v1.0.yaml").write_text(
        "epic: E\nversion: '1.0'\nspec_file_name: spec.xlsx\n"
        "tables:\n  - table_name: T\n",
        encoding="utf-8",
    )
    wb = Workbook()
    wb.active.title = "T"
    ws = wb["T"]
    ws.append(["Champ dans extract", "Type", "Description", "Obligatoire ?"])
    ws.append(["x", "Double", "d", "OUI"])
    add_keys_sheet(wb, [("T", "x", None)])
    wb.save(edir / "specs" / "spec.xlsx")

    rc = main(["generate", "--epic", "E"])
    assert rc == 0


def test_generate_skip_self_check_flag_is_accepted(repo_root, monkeypatch):
    """--skip-self-check should be tolerated and not break the run."""
    monkeypatch.chdir(repo_root)
    rc = main(["generate", "--epic", "1118", "--skip-self-check"])
    assert rc == 0


def test_self_check_directly_via_check_invariants_catches_dangling_fk(repo_root):
    """Direct unit test of the self-check engine: a Contract whose FK targets
    a peer-set entry that doesn't carry the referenced column should produce
    a fk_target_exists error."""
    from data_contract.contract import Contract, FieldContract
    from data_contract.type_mapping import Type, load_type_registry
    from data_contract.generation.validate_contract import check_invariants

    registry = load_type_registry(repo_root / "configs" / "types.yaml")
    contract = Contract(
        version="1.0", epic="E", generated_at="t",
        spec_file="s", spec_sheet="S", table="CHILD",
        fields=[FieldContract(
            name="parent_id", type=Type.INT64, nullable=False, description=None,
            foreign_key={"table": "PARENT", "column": "missing_col"},
        )],
    )
    peer_field_names = {"PARENT": {"parent_id"}}  # has parent_id, NOT missing_col
    errs = check_invariants(contract, registry, peer_field_names, allow_unknown_constraints=False)
    kinds = {e.kind for e in errs}
    assert "fk_target_exists" in kinds


def test_validate_contract_mutual_exclusive_file_and_epic(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    rc = main(["validate-contract", "--file", "x.yaml", "--epic", "E"])
    assert rc == 1


# ---------------------------------------------------------------------------
# P0.3 — --json output
# ---------------------------------------------------------------------------


def test_validate_contract_json_output_clean(tmp_path, repo_root, monkeypatch, capsys):
    import json as _json
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload())
    rc = main(["validate-contract", "--file", str(p), "--json"])
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["results"][0]["ok"] is True
    assert payload["results"][0]["table"] == "T"


def test_validate_contract_json_output_with_errors(tmp_path, repo_root, monkeypatch, capsys):
    import json as _json
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    _write_contract(p, _minimal_payload(primary_key=True, nullable=True))
    rc = main(["validate-contract", "--file", str(p), "--json"])
    assert rc == 2
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    res = payload["results"][0]
    kinds = {e["kind"] for e in res["invariant_errors"]}
    assert "pk_must_not_be_nullable" in kinds


def test_validate_contract_json_init_error(tmp_path, repo_root, monkeypatch, capsys):
    import json as _json
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    rc = main(["validate-contract", "--file", "x.yaml", "--epic", "E", "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert "mutually exclusive" in payload["error"]


# ---------------------------------------------------------------------------
# P0.5 — epic/version path invariants
# ---------------------------------------------------------------------------


def test_path_invariant_epic_mismatch_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    contracts = tmp_path / "epics" / "REAL_EPIC" / "contracts"
    payload = _minimal_payload()
    payload["epic"] = "WRONG_EPIC"  # YAML claims a different epic than its directory
    _write_contract(contracts / "T.yaml", payload)
    rc = main(["validate-contract", "--epic", "REAL_EPIC"])
    assert rc == 2
    assert "epic_matches_path" in capsys.readouterr().err


def test_path_invariant_history_version_mismatch_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    history = tmp_path / "epics" / "E" / "contracts" / "history" / "1.0"
    payload = _minimal_payload()
    payload["epic"] = "E"
    payload["version"] = "2.0"  # in 1.0/ but says 2.0
    _write_contract(history / "T.yaml", payload)
    rc = main(["validate-contract", "--file", str(history / "T.yaml")])
    assert rc == 2
    assert "version_matches_path" in capsys.readouterr().err


def test_path_invariant_skipped_when_outside_epics_tree(tmp_path, repo_root, monkeypatch):
    """A YAML somewhere random (not under epics/) shouldn't trigger path checks."""
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "random" / "T.yaml"
    payload = _minimal_payload()
    payload["epic"] = "anything"
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 0


# ---------------------------------------------------------------------------
# P0.2 — joins.yaml validation
# ---------------------------------------------------------------------------


def _build_epic_with_joins(tmp_path: Path, joins_payload: dict) -> Path:
    contracts = tmp_path / "epics" / "E" / "contracts"
    pk_payload = _minimal_payload(table="PARENT")
    pk_payload["epic"] = "E"
    pk_payload["fields"][0] = {"name": "parent_id", "type": "int64", "nullable": False, "primary_key": True}
    _write_contract(contracts / "PARENT.yaml", pk_payload)

    child_payload = _minimal_payload(table="CHILD")
    child_payload["epic"] = "E"
    child_payload["fields"][0] = {"name": "parent_id", "type": "int64", "nullable": False}
    _write_contract(contracts / "CHILD.yaml", child_payload)

    joins_payload.setdefault("epic", "E")
    _write_contract(contracts / "joins.yaml", joins_payload)
    return contracts


def test_validate_joins_clean(tmp_path, repo_root, monkeypatch):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0",
        "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "parent_id",
            "target_table": "CHILD", "target_column": "parent_id",
            "type": "LEFT", "cardinality": "1:n",
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 0


def test_validate_joins_unknown_target_table_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0", "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "parent_id",
            "target_table": "GHOST", "target_column": "x",
            "type": "LEFT",
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "joins_unknown_table" in capsys.readouterr().err


def test_validate_joins_unknown_source_column_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0", "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "no_such_field",
            "target_table": "CHILD", "target_column": "parent_id",
            "type": "LEFT",
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "joins_unknown_column" in capsys.readouterr().err


def test_validate_joins_unknown_join_type_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0", "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "parent_id",
            "target_table": "CHILD", "target_column": "parent_id",
            "type": "BOGUS",
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "joins_unknown_type" in capsys.readouterr().err


def test_validate_joins_invalid_cardinality_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0", "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "parent_id",
            "target_table": "CHILD", "target_column": "parent_id",
            "type": "LEFT", "cardinality": "many:many",
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "joins_unknown_cardinality" in capsys.readouterr().err


def test_validate_joins_missing_required_field_fails(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    _build_epic_with_joins(tmp_path, {
        "version": "1.0", "table": "joins",
        "joins": [{
            "source_table": "PARENT", "source_column": "parent_id",
            # missing target_table, target_column, type
        }],
    })
    rc = main(["validate-contract", "--epic", "E"])
    assert rc == 2
    assert "joins_missing_required" in capsys.readouterr().err


def test_validate_contract_min_max_consistency(tmp_path, repo_root, monkeypatch, capsys):
    _bootstrap_validate_layout(tmp_path, repo_root)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "T.yaml"
    payload = _minimal_payload(type="float64")
    payload["fields"][0]["min_value"] = {"value": 100, "strict": False}
    payload["fields"][0]["max_value"] = {"value": 50, "strict": False}
    _write_contract(p, payload)
    rc = main(["validate-contract", "--file", str(p)])
    assert rc == 2
    assert "min_value_max_value_consistency" in capsys.readouterr().err
