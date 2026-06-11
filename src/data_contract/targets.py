"""Per-database target overlays for validation.

A target config carries overrides keyed by universal canonical type. The
overrides drive `value_parsers` (bounds, parse_formats), the BOOLEAN token
map, and `check_max_length`'s unit (bytes vs characters).

Path resolution: a per-epic target file (epics/<E>/configs/targets/<n>.yaml)
beats the repo-root file (configs/targets/<n>.yaml). Returns ConfigError when
the target is not declared in either location.

Validation rules are loaded at startup; the runner overlays them onto the base
`TypeRegistry` via `TypeRegistry.with_target(target)` before passing the
registry through to the per-table checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError
from data_contract.type_mapping import Type


# Which override keys are accepted on which canonical. Misplacement raises
# `ConfigError` at load time, same typo-gate pattern as `parse_formats`.
# `physical_type` is allowed on every canonical -- it's the target's name for
# the type and is REQUIRED whenever an overrides entry is declared.
_OVERRIDE_ALLOWLIST: dict[str, frozenset[Type]] = {
    "physical_type": frozenset({
        Type.STRING, Type.TEXT, Type.INT32, Type.INT64, Type.FLOAT32, Type.FLOAT64,
        Type.DECIMAL, Type.BOOLEAN, Type.DATE, Type.TIMESTAMP,
        Type.TIMESTAMP_TZ, Type.BINARY,
    }),
    "bounds":        frozenset({Type.INT32, Type.INT64, Type.FLOAT32, Type.FLOAT64, Type.DECIMAL}),
    "max_precision": frozenset({Type.DECIMAL}),
    "length_unit":   frozenset({Type.STRING}),
    # `data_values` is intentionally not listed: boolean tokens live on each
    # contract field (stamped from configs/types.yaml at generation time),
    # not on the target. Declaring `data_values:` on a target is a config
    # mistake and raises `ConfigError` at load.
    "parse_formats": frozenset({Type.DATE, Type.TIMESTAMP, Type.TIMESTAMP_TZ}),
}

_VALID_LENGTH_UNITS = frozenset({"bytes", "characters"})


@dataclass(frozen=True)
class TargetOverrides:
    """Per-canonical override slot.

    `physical_type` is REQUIRED when the entry exists -- it's the target's
    name for the canonical (e.g. `VARCHAR2({max_length} BYTE)` on Oracle for
    string). Other fields are optional. Templating placeholders in
    `physical_type`:
      - `{max_length}` -> field.max_length
      - `{precision}`  -> field.precision
      - `{scale}`      -> field.scale
    Substituted by `TypeRegistry.physical_type_for(field)`.
    """
    physical_type: str = ""                     # required at load time
    bounds: tuple[Any, Any] | None = None
    max_precision: int | None = None
    length_unit: str | None = None              # "bytes" | "characters"
    parse_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetConfig:
    name: str
    description: str
    overrides: dict[Type, TargetOverrides] = field(default_factory=dict)


def resolve_target_path(
    target_name: str, *, repo_root: Path, epic_dir: Path | None,
) -> Path:
    """Find the YAML file for `target_name`. Per-epic wins over repo-root."""
    candidates: list[Path] = []
    if epic_dir is not None:
        candidates.append(epic_dir / "configs" / "targets" / f"{target_name}.yaml")
    candidates.append(repo_root / "configs" / "targets" / f"{target_name}.yaml")
    for p in candidates:
        if p.is_file():
            return p
    raise ConfigError(
        f"target {target_name!r} not found; searched: "
        f"{', '.join(str(c) for c in candidates)}"
    )


def load_target_config(path: Path) -> TargetConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}: `name` must be a non-empty string")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{path}: `description` must be a string")

    overrides_raw = raw.get("overrides")
    if overrides_raw is not None and not isinstance(overrides_raw, dict):
        raise ConfigError(f"{path}: `overrides` must be a mapping")
    overrides_by_type: dict[Type, TargetOverrides] = {}
    for canonical_name, body in (overrides_raw or {}).items():
        try:
            canonical = Type.from_canonical_string(canonical_name)
        except ValueError:
            raise ConfigError(
                f"{path}: overrides.{canonical_name!r} is not a recognized canonical"
            ) from None
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: overrides.{canonical_name} body must be a mapping")
        overrides_by_type[canonical] = _parse_overrides_body(
            body, canonical=canonical, path=path,
        )

    return TargetConfig(name=name, description=description, overrides=overrides_by_type)


def _parse_overrides_body(
    body: dict, *, canonical: Type, path: Path,
) -> TargetOverrides:
    physical_type: str = ""
    bounds: tuple[Any, Any] | None = None
    max_precision: int | None = None
    length_unit: str | None = None
    parse_formats: tuple[str, ...] = ()

    unknown = sorted(set(body) - set(_OVERRIDE_ALLOWLIST))
    if unknown:
        raise ConfigError(
            f"{path}: overrides.{canonical.value}: unknown keys {unknown}; "
            f"accepted: {sorted(_OVERRIDE_ALLOWLIST)}"
        )

    # `physical_type` is mandatory for every overrides entry -- no defaults,
    # explicit per-canonical naming required.
    if "physical_type" not in body:
        raise ConfigError(
            f"{path}: overrides.{canonical.value} is missing required key 'physical_type'; "
            f"declare the target's physical type name (use {{max_length}} / "
            f"{{precision}} / {{scale}} placeholders for parameterised types)"
        )
    pt_raw = body["physical_type"]
    if not isinstance(pt_raw, str) or not pt_raw:
        raise ConfigError(
            f"{path}: overrides.{canonical.value}.physical_type must be a non-empty string"
        )
    physical_type = pt_raw

    for key in body:
        if canonical not in _OVERRIDE_ALLOWLIST[key]:
            raise ConfigError(
                f"{path}: overrides.{canonical.value}.{key} is not allowed on this canonical; "
                f"valid canonicals for {key!r}: "
                f"{sorted(t.value for t in _OVERRIDE_ALLOWLIST[key])}"
            )

    if "bounds" in body:
        bounds = _parse_bounds(body["bounds"], canonical=canonical, path=path)

    if "max_precision" in body:
        mp = body["max_precision"]
        if not isinstance(mp, int) or isinstance(mp, bool) or mp < 1:
            raise ConfigError(
                f"{path}: overrides.{canonical.value}.max_precision must be a positive integer"
            )
        max_precision = mp

    if "length_unit" in body:
        unit = body["length_unit"]
        if unit not in _VALID_LENGTH_UNITS:
            raise ConfigError(
                f"{path}: overrides.{canonical.value}.length_unit must be one of "
                f"{sorted(_VALID_LENGTH_UNITS)}; got {unit!r}"
            )
        length_unit = unit

    if "parse_formats" in body:
        from data_contract.type_mapping import _parse_parse_formats
        parse_formats = _parse_parse_formats(
            body["parse_formats"], canonical=canonical.value, path=path,
        )

    return TargetOverrides(
        physical_type=physical_type,
        bounds=bounds,
        max_precision=max_precision,
        length_unit=length_unit,
        parse_formats=parse_formats,
    )


def _parse_bounds(raw: Any, *, canonical: Type, path: Path) -> tuple[Any, Any]:
    """Bounds may be int or string (string lets YAML carry Oracle-NUMBER(38)
    values without int-overflow on parse). For decimal we coerce to Decimal;
    for int / float we coerce to int / float respectively.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: overrides.{canonical.value}.bounds must be a mapping with min / max"
        )
    if set(raw) != {"min", "max"}:
        raise ConfigError(
            f"{path}: overrides.{canonical.value}.bounds must have exactly keys 'min' and 'max'; "
            f"got {sorted(raw)}"
        )
    return _coerce_bound(raw["min"], canonical, "min", path), _coerce_bound(raw["max"], canonical, "max", path)


def _coerce_bound(raw: Any, canonical: Type, which: str, path: Path) -> Any:
    if canonical is Type.DECIMAL:
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            raise ConfigError(
                f"{path}: overrides.decimal.bounds.{which} must be a valid decimal; got {raw!r}"
            ) from None
    if canonical in (Type.INT32, Type.INT64):
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ConfigError(
                f"{path}: overrides.{canonical.value}.bounds.{which} must be an integer; got {raw!r}"
            ) from None
    # float32 / float64
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{path}: overrides.{canonical.value}.bounds.{which} must be numeric; got {raw!r}"
        ) from None
