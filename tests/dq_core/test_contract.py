"""Round-trip + key-rejection tests for the v2 three-layer name layout
on `FieldContract`. Step 3 of the plan
(plans/plan-a-three-layer-name-rustling-pond.md)."""

from __future__ import annotations

import pytest

from dq_core.contract import FieldContract
from dq_core.errors import ConfigError
from dq_core.type_mapping import Type


def _base_payload(**overrides):
    payload = {"name": "record_number", "type": "string", "nullable": True}
    payload.update(overrides)
    return payload


def test_to_dict_emits_all_three_names_when_they_differ():
    f = FieldContract(
        name="record_number",
        extract_name="Record Number",
        bronze_name="record no",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert out["name"] == "record_number"
    assert out["extract_name"] == "Record Number"
    assert out["bronze_name"] == "record no"
    # Stable order: name, extract_name, bronze_name, type, ...
    assert list(out.keys())[:4] == ["name", "extract_name", "bronze_name", "type"]


def test_to_dict_omits_extract_name_equal_to_name():
    f = FieldContract(
        name="email", extract_name="email",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert "extract_name" not in out


def test_to_dict_omits_bronze_name_equal_to_name():
    f = FieldContract(
        name="email", bronze_name="email",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert "bronze_name" not in out


def test_to_dict_omits_both_when_only_name_set():
    f = FieldContract(name="email", type=Type.STRING, nullable=True, description=None)
    out = f.to_dict()
    assert "extract_name" not in out
    assert "bronze_name" not in out


def test_from_dict_accepts_extract_and_bronze_names():
    f = FieldContract.from_dict(_base_payload(
        extract_name="Record Number",
        bronze_name="record no",
    ))
    assert f.name == "record_number"
    assert f.extract_name == "Record Number"
    assert f.bronze_name == "record no"


def test_from_dict_rejects_v1_source_name_with_actionable_message():
    """Hard cutover: the v1 `source_name:` key fails loudly with a pointer
    at the migrate-names tool. No compat alias."""
    payload = _base_payload(source_name="Record Number")
    with pytest.raises(ConfigError, match=r"source_name.*migrate-names"):
        FieldContract.from_dict(payload)


def test_round_trip_preserves_three_names():
    original = FieldContract(
        name="record_number",
        extract_name="Record Number",
        bronze_name="record no",
        type=Type.STRING, nullable=True, description="id",
    )
    restored = FieldContract.from_dict(original.to_dict())
    assert restored.name == original.name
    assert restored.extract_name == original.extract_name
    assert restored.bronze_name == original.bronze_name


def test_round_trip_silver_only_stays_silver_only():
    """When the field only has a silver name, the YAML emission omits the
    other two slots; from_dict restores both as None."""
    original = FieldContract(name="email", type=Type.STRING, nullable=True, description=None)
    restored = FieldContract.from_dict(original.to_dict())
    assert restored.extract_name is None
    assert restored.bronze_name is None
