"""YAML I/O helpers used by every config loader.

Single source of truth for:
  - canonical dump formatting (UTF-8, LF, no flow style, no key-sorting)
  - permissive load (missing/empty file -> {})
  - load + require-mapping (raises ConfigError on non-dict top level)

Replaces the inline `yaml.safe_load(path.read_text(...))` plus the
duplicated "top-level YAML must be a mapping" error message that used
to live in seven modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning an empty dict on missing/empty payload.

    Non-mapping payloads (a YAML list at top level, a bare scalar) also
    return {}. Callers that need to reject those shapes should use
    `load_yaml_mapping` instead.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def load_yaml_mapping(path: Path, *, what: str = "file") -> dict[str, Any]:
    """Load a YAML file that MUST have a mapping at the top level.

    `what` is interpolated into the error message ("validation config",
    "type registry", ...). Missing file is the caller's problem -- raise
    upfront with `path.is_file()` if needed.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping ({what})")
    return raw


def dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write a YAML file with the project's canonical formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(
            payload,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
