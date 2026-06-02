"""Small cross-module helpers. Kept dependency-free so any module can import."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now_iso_z() -> str:
    """UTC timestamp in ISO-8601 form, second-precision, trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning an empty dict on missing/empty payload."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}
