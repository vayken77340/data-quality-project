"""Back-compat re-exports.

Everything `_util` used to host has moved to `data_contract.core/`:
  * YAML I/O    -> `data_contract.core.yaml_io`
  * Epic-name   -> `data_contract.core.epic`

`now_iso_z` stays here because it has nothing to share with anything else
and lives in the namespace every module already imports from.
"""

from __future__ import annotations

from datetime import datetime, timezone

from data_contract.core.epic import InvalidEpicName, validate_epic_name
from data_contract.core.yaml_io import dump_yaml, load_yaml


def now_iso_z() -> str:
    """UTC timestamp in ISO-8601 form, second-precision, trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "now_iso_z",
    # Re-exports
    "load_yaml",
    "dump_yaml",
    "InvalidEpicName",
    "validate_epic_name",
]
