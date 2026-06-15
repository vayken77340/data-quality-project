"""Runtime feature flags read from `.env` at the project root.

Generation-side flags (steer optional steps of `data-contract generate`):
  - allow_foreign_key_violation: demote unresolved FK references from
    rejection to warning.
  - allow_missing_primary_keys: tolerate tables that have no row in the
    keys sheet. The contract still emits (with no `primary_key` fields)
    and the data validator's pk_uniqueness check has nothing to enforce,
    so duplicate rows in the sample become tolerated. Default False --
    the keys sheet remains the source of truth unless explicitly relaxed.
  - generate_history: write versioned `contracts/history/<v>/<table>.yaml`
    snapshots alongside the canonical contract.
  - generate_drift: emit `contracts/drift/<table>__v<a>_to_v<b>.yaml`
    when a new history snapshot follows an existing older one.
  - generate_join_contract: read the joins sheet and emit `joins.yaml`.

Validation-side flags (steer `data-contract validate-data`):
  - rejected_row_cap: how many distinct (source_file, source_row) tuples
    the rejected-rows sheet surfaces before truncating. Tunable per
    operator -- CI wants a tight cap for fast feedback; interactive
    debugging wants headroom. Default 500.
  - extra_columns_severity: severity for columns present in the data file
    but not declared in the contract. One of error|warning|info|ignore.
    Default "warning". `--strict-columns` on the CLI still overrides to
    "error" for one-off runs.

All flags default to sensible values when `.env` is absent or the key is
unset, so a fresh checkout / clean tmp_path / test sandbox gets the same
behavior as a configured project. Boolean values parse permissively
(`true`/`1`/`yes`/`on` -> True; anything else -> False).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_ENV_FILENAME = ".env"
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_VALID_EXTRA_COLUMN_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})


@dataclass(frozen=True)
class Settings:
    # Generation-side
    allow_foreign_key_violation: bool = True
    allow_missing_primary_keys: bool = False
    generate_history: bool = True
    generate_drift: bool = True
    generate_join_contract: bool = True
    # Validation-side
    rejected_row_cap: int = 500
    extra_columns_severity: str = "warning"
    # Global similarity threshold used by parsers whose `field_matching_policy`
    # is "similarity". One value across all parsers (csv, excel, ...) so the
    # operator tunes it in one place; per-parser tuning was deliberately
    # rejected as a footgun. Range: 0..1; 0.8 strikes a reasonable balance
    # between catching typos / case / spacing variants and avoiding false
    # matches across genuinely-different columns.
    similarity_threshold: float = 0.8


class SettingsError(ValueError):
    """Raised when a `.env` value fails validation (bad severity, non-integer cap, ...)."""


def _strip_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return _strip_quotes(value).lower() in _TRUTHY


def _parse_int(value: str | None, default: int, *, key: str, minimum: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(_strip_quotes(value))
    except ValueError as e:
        raise SettingsError(f"{_ENV_FILENAME}: {key}={value!r} must be an integer") from e
    if parsed < minimum:
        raise SettingsError(f"{_ENV_FILENAME}: {key}={parsed} must be >= {minimum}")
    return parsed


def _parse_float_in_unit_range(value: str | None, default: float, *, key: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(_strip_quotes(value))
    except ValueError as e:
        raise SettingsError(f"{_ENV_FILENAME}: {key}={value!r} must be a number") from e
    if not 0.0 <= parsed <= 1.0:
        raise SettingsError(f"{_ENV_FILENAME}: {key}={parsed} must be in [0, 1]")
    return parsed


def _parse_choice(
    value: str | None, default: str, *, key: str, choices: frozenset[str],
) -> str:
    if value is None:
        return default
    stripped = _strip_quotes(value).lower()
    if stripped not in choices:
        raise SettingsError(
            f"{_ENV_FILENAME}: {key}={value!r} must be one of {sorted(choices)}"
        )
    return stripped


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

    Missing file or missing keys fall back to the dataclass defaults. Bad
    values raise `SettingsError` so a typo in `.env` fails the run rather
    than silently using a default.
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
        allow_missing_primary_keys=_parse_bool(
            values.get("allow_missing_primary_keys"), defaults.allow_missing_primary_keys,
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
        rejected_row_cap=_parse_int(
            values.get("rejected_row_cap"), defaults.rejected_row_cap,
            key="rejected_row_cap", minimum=1,
        ),
        extra_columns_severity=_parse_choice(
            values.get("extra_columns_severity"), defaults.extra_columns_severity,
            key="extra_columns_severity", choices=_VALID_EXTRA_COLUMN_SEVERITIES,
        ),
        similarity_threshold=_parse_float_in_unit_range(
            values.get("similarity_threshold"), defaults.similarity_threshold,
            key="similarity_threshold",
        ),
    )
