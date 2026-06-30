"""Round-trip + key-rejection tests for the v3 three-layer name layout
on `FieldContract`. Addendum to Follow-up 2: always-emit -- every field
carries silver_name + extract_name + bronze_name; no equality-based
omission."""

from __future__ import annotations

import pytest

from dq_core.contract import FieldContract
from dq_core.errors import ConfigError
from dq_core.type_mapping import Type


def _base_payload(**overrides):
    payload = {
        "silver_name": "record_number",
        "extract_name": "record_number",
        "bronze_name": "record_number",
        "type": "string",
        "nullable": True,
    }
    payload.update(overrides)
    return payload


def test_to_dict_emits_all_three_names_when_they_differ():
    f = FieldContract(
        silver_name="record_number",
        extract_name="Record Number",
        bronze_name="record no",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert out["silver_name"] == "record_number"
    assert out["extract_name"] == "Record Number"
    assert out["bronze_name"] == "record no"
    # Stable canonical order: silver_name, extract_name, bronze_name, type, ...
    assert list(out.keys())[:4] == ["silver_name", "extract_name", "bronze_name", "type"]


def test_to_dict_always_emits_extract_name_even_when_equal_to_silver():
    """Always-emit: the 'stay quiet' invariant is dropped."""
    f = FieldContract(
        silver_name="email", extract_name="email",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert out["extract_name"] == "email"


def test_to_dict_always_emits_bronze_name_even_when_equal_to_silver():
    f = FieldContract(
        silver_name="email", bronze_name="email",
        type=Type.STRING, nullable=True, description=None,
    )
    out = f.to_dict()
    assert out["bronze_name"] == "email"


def test_to_dict_emits_all_three_slots_when_only_silver_provided():
    """Always-emit + dataclass __post_init__: extract_name and bronze_name
    materialize to silver_name when omitted at construction."""
    f = FieldContract(silver_name="email", type=Type.STRING, nullable=True, description=None)
    out = f.to_dict()
    assert out["silver_name"] == "email"
    assert out["extract_name"] == "email"
    assert out["bronze_name"] == "email"


def test_from_dict_accepts_extract_and_bronze_names():
    f = FieldContract.from_dict(_base_payload(
        extract_name="Record Number",
        bronze_name="record no",
    ))
    assert f.silver_name == "record_number"
    assert f.extract_name == "Record Number"
    assert f.bronze_name == "record no"


def test_from_dict_rejects_v1_source_name_with_actionable_message():
    """Hard cutover: the v1 `source_name:` key fails loudly with a pointer
    at the migrate-names tool. No compat alias."""
    payload = _base_payload(source_name="Record Number")
    with pytest.raises(ConfigError, match=r"source_name.*migrate-names"):
        FieldContract.from_dict(payload)


def test_from_dict_rejects_v2_name_with_actionable_message():
    """v2 -> v3 cutover: the v2 `name:` key fails loudly."""
    payload = {"name": "record_number", "type": "string", "nullable": True}
    with pytest.raises(ConfigError, match=r"unrecognized field key 'name'.*migrate-names"):
        FieldContract.from_dict(payload)


def test_from_dict_legacy_key_rejection_runs_before_missing_required_key_check():
    """Rejection precedence: legacy-key first, missing-required second.
    Operators with pre-cutover contracts see the migrate-names hint before
    the missing-required-key complaint."""
    payload = {"name": "x", "type": "string", "nullable": True}  # name (legacy) + missing silver/extract/bronze
    with pytest.raises(ConfigError, match=r"unrecognized field key 'name'"):
        FieldContract.from_dict(payload)


def test_from_dict_missing_extract_name_rejects():
    payload = {"silver_name": "x", "bronze_name": "x", "type": "string", "nullable": True}
    with pytest.raises(ConfigError, match=r"missing required key 'extract_name'.*migrate-names"):
        FieldContract.from_dict(payload)


def test_from_dict_missing_bronze_name_rejects():
    payload = {"silver_name": "x", "extract_name": "x", "type": "string", "nullable": True}
    with pytest.raises(ConfigError, match=r"missing required key 'bronze_name'.*migrate-names"):
        FieldContract.from_dict(payload)


def test_round_trip_preserves_three_names():
    original = FieldContract(
        silver_name="record_number",
        extract_name="Record Number",
        bronze_name="record no",
        type=Type.STRING, nullable=True, description="id",
    )
    restored = FieldContract.from_dict(original.to_dict())
    assert restored.silver_name == original.silver_name
    assert restored.extract_name == original.extract_name
    assert restored.bronze_name == original.bronze_name


def test_round_trip_silver_only_materializes_all_three():
    """Construct with only silver_name -> dataclass materializes extract /
    bronze to silver -> to_dict emits all three -> from_dict round-trips
    cleanly."""
    original = FieldContract(silver_name="email", type=Type.STRING, nullable=True, description=None)
    restored = FieldContract.from_dict(original.to_dict())
    assert restored.silver_name == "email"
    assert restored.extract_name == "email"
    assert restored.bronze_name == "email"
