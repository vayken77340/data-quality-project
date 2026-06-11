"""JSON Schema export for the contract YAML.

The schema is derived from `Contract` / `FieldContract` dataclass annotations
in [contract.py](contract.py) plus each registered constraint's
`contract_value_schema()`. Built-in constraints declare static fragments via
`CONTRACT_VALUE_SCHEMA`; `format` overrides the classmethod to surface the
current `FORMAT_REGISTRY` tokens as an enum.

The schema is JSON Schema Draft 2020-12 and is published at
`docs/contract-schema.json`. The `validate-contract` CLI verb consumes it as
its syntactic pass; external tools (IDE YAML schemas, dbt-style preprocessors)
consume it directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from data_contract.field_constraints import REGISTRY
from data_contract.type_mapping import Type


SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
DEFAULT_SCHEMA_OUT = Path("docs/contract-schema.json")


def build_contract_json_schema() -> dict[str, Any]:
    """Construct the JSON Schema for a contract YAML.

    Returns a dict (not JSON-encoded) ready for `json.dump` or
    `jsonschema.validate`.
    """
    type_enum = [t.value for t in Type]

    field_properties: dict[str, Any] = {
        "name":         {"type": "string", "minLength": 1},
        "type":         {"type": "string", "enum": type_enum},
        "nullable":     {"type": "boolean"},
        "description":  {"type": "string"},
        "max_length":   {"type": "integer", "minimum": 0},
        "precision":    {"type": "integer", "minimum": 0},
        "scale":        {"type": "integer", "minimum": 0},
        "primary_key":  {"type": "boolean"},
        "foreign_key": {
            "type": "object",
            "properties": {
                "table":  {"type": "string"},
                "column": {"type": "string"},
            },
            "required": ["table", "column"],
            "additionalProperties": False,
        },
        # Per-field accepted-token map. Today only BOOLEAN fields populate
        # this (keys "true"/"false", values are arrays of raw tokens). Kept
        # generic so other canonicals could declare their own token maps later.
        "data_values": {
            "type": "object",
            "patternProperties": {
                "^.+$": {"type": "array", "items": {"type": ["string", "number", "boolean"]}},
            },
            "additionalProperties": False,
        },
    }

    # Each registered constraint contributes its contract_key to the Field
    # properties block, with the per-constraint value schema.
    for cls in REGISTRY.values():
        field_properties[cls.contract_key] = cls.contract_value_schema()

    return {
        "$schema": SCHEMA_DRAFT,
        "title": "DataContract",
        "type": "object",
        "required": ["version", "epic", "table", "fields"],
        "properties": {
            "version":      {"type": "string"},
            "epic":         {"type": "string"},
            "generated_at": {"type": "string"},
            "source": {
                "type": "object",
                "properties": {
                    "spec_file":  {"type": "string"},
                    "spec_sheet": {"type": "string"},
                },
            },
            "table":  {"type": "string", "minLength": 1},
            "fields": {
                "type": "array",
                "items": {"$ref": "#/$defs/Field"},
            },
        },
        "additionalProperties": False,
        "$defs": {
            "Field": {
                "type": "object",
                "required": ["name", "type"],
                "properties": field_properties,
                "additionalProperties": False,
            },
        },
    }


def write_contract_json_schema(out: Path = DEFAULT_SCHEMA_OUT) -> bool:
    """Render and write the schema. Returns True iff the file content changed
    (idempotent: same content -> no write, no mtime touch)."""
    schema = build_contract_json_schema()
    rendered = json.dumps(schema, indent=2, sort_keys=False) + "\n"
    if out.exists() and out.read_text(encoding="utf-8") == rendered:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return True


def validate_against_schema(contract_dict: dict) -> list[str]:
    """Validate a contract dict against the built schema.

    Returns a list of error strings, empty on success.
    """
    schema = build_contract_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for err in validator.iter_errors(contract_dict):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return errors
