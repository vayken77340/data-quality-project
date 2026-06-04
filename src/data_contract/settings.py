"""Runtime feature flags read from `.env` at the project root.

Four flags steer optional steps of `data-contract generate`:

  - allow_foreign_key_violation: demote unresolved FK references from
    rejection to warning (replaces the legacy
    `keys.foreign_key.allow_violations` block in defaults.yaml).
  - generate_history: write versioned `contracts/history/<v>/<table>.yaml`
    snapshots alongside the canonical contract.
  - generate_drift: emit `contracts/drift/<table>__v<a>_to_v<b>.yaml`
    when a new history snapshot follows an existing older one.
  - generate_join_contract: read the joins sheet and emit `joins.yaml`.

All four default to True when `.env` is absent or the key is unset, so
tests that run inside a clean tmp_path get the same behavior as a fresh
project checkout. Values are parsed permissively
(`true`/`1`/`yes`/`on` -> True; anything else -> False).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_ENV_FILENAME = ".env"
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})


@dataclass(frozen=True)
class Settings:
    allow_foreign_key_violation: bool = True
    generate_history: bool = True
    generate_drift: bool = True
    generate_join_contract: bool = True


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().strip('"').strip("'").lower() in _TRUTHY


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _find_env_file(start: Path) -> Path | None:
    """Walk from `start` up to the filesystem root, returning the first `.env` found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        env_path = candidate / _ENV_FILENAME
        if env_path.is_file():
            return env_path
    return None


def load_settings(start_dir: Path | None = None) -> Settings:
    """Load `Settings` from the nearest `.env` walking up from `start_dir` (default: cwd).

    Missing file or missing keys fall back to the dataclass defaults.
    """
    start = start_dir or Path.cwd()
    env_path = _find_env_file(start)
    if env_path is None:
        return Settings()
    values = _parse_env_file(env_path)
    defaults = Settings()
    return Settings(
        allow_foreign_key_violation=_parse_bool(
            values.get("allow_foreign_key_violation"), defaults.allow_foreign_key_violation,
        ),
        generate_history=_parse_bool(
            values.get("generate_history"), defaults.generate_history,
        ),
        generate_drift=_parse_bool(
            values.get("generate_drift"), defaults.generate_drift,
        ),
        generate_join_contract=_parse_bool(
            values.get("generate_join_contract"), defaults.generate_join_contract,
        ),
    )
