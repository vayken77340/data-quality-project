from __future__ import annotations

import json
from pathlib import Path

import yaml

from dq_core.field_constraints import REGISTRY
from dq_core.field_constraints.format import FORMAT_REGISTRY
from data_contract.generation.schema_export import (
    build_contract_json_schema,
    validate_against_schema,
    write_contract_json_schema,
)


def test_schema_has_all_registered_constraint_keys():
    schema = build_contract_json_schema()
    field_props = schema["$defs"]["Field"]["properties"]
    expected = {cls.contract_key for cls in REGISTRY.values()}
    assert expected.issubset(field_props.keys())


def test_schema_min_value_fragment_is_structured():
    schema = build_contract_json_schema()
    fragment = schema["$defs"]["Field"]["properties"]["min_value"]
    assert fragment["type"] == "object"
    assert set(fragment["required"]) == {"value", "strict"}
    assert "strict" in fragment["properties"]
    assert fragment["properties"]["strict"]["type"] == "boolean"


def test_schema_max_value_fragment_is_structured():
    schema = build_contract_json_schema()
    fragment = schema["$defs"]["Field"]["properties"]["max_value"]
    assert fragment["type"] == "object"


def test_schema_format_enum_lists_registered_tokens():
    schema = build_contract_json_schema()
    fragment = schema["$defs"]["Field"]["properties"]["format"]
    assert fragment["type"] == "string"
    assert set(fragment["enum"]) == set(FORMAT_REGISTRY)


def test_schema_unique_constraint_is_const_true():
    schema = build_contract_json_schema()
    fragment = schema["$defs"]["Field"]["properties"]["unique"]
    assert fragment == {"type": "boolean", "const": True}


def test_schema_allowed_values_is_array():
    schema = build_contract_json_schema()
    fragment = schema["$defs"]["Field"]["properties"]["allowed_values"]
    assert fragment["type"] == "array"
    assert fragment["items"] == {"type": "string"}   # JSON Schema's string, not contract's varchar


def test_schema_field_disallows_extra_properties():
    schema = build_contract_json_schema()
    assert schema["$defs"]["Field"]["additionalProperties"] is False


def test_schema_top_level_disallows_extra_properties():
    schema = build_contract_json_schema()
    assert schema["additionalProperties"] is False


def test_schema_required_top_level_keys():
    schema = build_contract_json_schema()
    assert set(schema["required"]) == {"version", "epic", "table", "fields"}


def test_write_schema_idempotent(tmp_path: Path):
    out = tmp_path / "schema.json"
    assert write_contract_json_schema(out) is True   # first write
    assert out.exists()
    assert write_contract_json_schema(out) is False  # second write — no change


def test_validate_against_schema_accepts_real_epic_1118_contract(repo_root: Path):
    contract_path = repo_root / "epics" / "1118" / "contracts" / "ipn_project.yaml"
    contract_dict = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    errors = validate_against_schema(contract_dict)
    assert errors == [], f"unexpected schema errors: {errors}"


def test_validate_against_schema_rejects_unknown_field_property():
    bad_contract = {
        "version": "1.0",
        "epic": "X",
        "generated_at": "t",
        "spec": {"file_path": "s", "sheet_name": "S"},
        "table": "T",
        "fields": [
            {"silver_name": "x", "extract_name": "x", "bronze_name": "x", "type": "int64", "bogus_field": "nope"},
        ],
    }
    errors = validate_against_schema(bad_contract)
    assert any("bogus_field" in e for e in errors), f"expected error about bogus_field; got {errors}"


def test_validate_against_schema_rejects_unknown_type():
    bad_contract = {
        "version": "1.0", "epic": "X", "generated_at": "t",
        "spec": {"file_path": "s", "sheet_name": "S"},
        "table": "T",
        "fields": [{"silver_name": "x", "extract_name": "x", "bronze_name": "x", "type": "quaternion"}],
    }
    errors = validate_against_schema(bad_contract)
    assert any("type" in e for e in errors)


def test_validate_against_schema_accepts_structured_min_value():
    contract = {
        "version": "1.0", "epic": "X", "generated_at": "t",
        "spec": {"file_path": "s", "sheet_name": "S"},
        "table": "T",
        "fields": [
            {
                "silver_name": "amount", "extract_name": "amount", "bronze_name": "amount",
                "type": "float64",
                "min_value": {"value": 0, "strict": False},
            }
        ],
    }
    assert validate_against_schema(contract) == []


def test_validate_against_schema_rejects_flat_min_value():
    """Legacy flat min_value: 5 must fail the structured-shape constraint."""
    contract = {
        "version": "1.0", "epic": "X", "generated_at": "t",
        "spec": {"file_path": "s", "sheet_name": "S"},
        "table": "T",
        "fields": [{"silver_name": "amount", "extract_name": "amount", "bronze_name": "amount", "type": "float64", "min_value": 5}],
    }
    errors = validate_against_schema(contract)
    assert errors, "flat min_value should be rejected"


def test_export_schema_cli_writes_file(tmp_path: Path):
    from data_contract.cli import main
    out = tmp_path / "schema.json"
    rc = main(["export-schema", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["title"] == "DataContract"
