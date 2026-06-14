"""Tier-keyed gate parser shared by validation.yaml's `checks:` and `metrics:`.

Both blocks have the same structure: a tier-keyed mapping of name -> bool
(or `{enabled: bool}` for forward compat). The parser:

  * top-level requires every tier to be exhaustive (every registered name
    must be explicitly set, so a forgotten plugin can't silently default to
    on/off);
  * per-table overrides are partial (any tier or name may be omitted);
  * names placed in the wrong tier raise a "wrong tier" ConfigError.

The result is a flat `Gates` carrying both `specs[name] -> enabled` and
`tier_of[name] -> tier`. The runner only reads `is_enabled(name)`; the
tier index is preserved so override errors can mention the correct tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data_contract.errors import ConfigError


@dataclass(frozen=True)
class GateSpec:
    """Per-name spec. Today carries just `enabled`; the dataclass shape
    leaves room for future per-name options (severity overrides, etc.)."""
    enabled: bool


@dataclass(frozen=True)
class Gates:
    """Flat per-name gate map plus a tier index.

    `specs[name]` carries the GateSpec for each gate; `tier_of[name]`
    records which tier the entry came from. The runner only reads
    `specs` via `is_enabled(name)`.
    """
    specs: dict[str, GateSpec] = field(default_factory=dict)
    tier_of: dict[str, str] = field(default_factory=dict)

    def is_enabled(self, name: str) -> bool:
        spec = self.specs.get(name)
        return spec.enabled if spec is not None else True

    def with_overrides(self, override: "Gates") -> "Gates":
        """Merge `override` on top of self. Per-table beats global key by key."""
        merged_specs: dict[str, GateSpec] = {**self.specs, **override.specs}
        merged_tier: dict[str, str] = {**self.tier_of, **override.tier_of}
        return Gates(specs=merged_specs, tier_of=merged_tier)

    @property
    def values(self) -> dict[str, bool]:
        return {name: spec.enabled for name, spec in self.specs.items()}


def parse_tier_gates(
    raw: Any,
    *,
    ctx: str,
    block_label: str,                       # "checks" or "metrics"
    tier_keys: tuple[str, ...],
    tier_names: dict[str, frozenset[str]],  # tier -> valid names in that tier
    require_complete: bool,
) -> Gates:
    """Parse a tier-keyed block.

    `require_complete=True`:
      * every tier key must be present
      * within each tier every registered name must be present
    `require_complete=False` (per-table override):
      * any tier may be omitted
      * within a tier any name may be omitted
      * names placed in the wrong tier raise with the correct tier named
    """
    if raw is None:
        if require_complete:
            blueprint = _format_tier_blueprint(tier_keys, tier_names)
            raise ConfigError(
                f"{ctx}: top-level '{block_label}' block is required and must list "
                f"every tier with every registered name. Expected shape:\n{blueprint}"
            )
        return Gates()

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{ctx}: '{block_label}' must be a tier-keyed mapping with tier keys "
            f"{list(tier_keys)}; got {type(raw).__name__}"
        )

    unknown_tiers = sorted(set(raw) - set(tier_keys))
    if unknown_tiers:
        # If an unknown "tier" is actually a known name, suggest the right tier.
        suggestions = []
        for bad in unknown_tiers:
            for tier, names in tier_names.items():
                if bad in names:
                    suggestions.append(f"{bad!r} belongs under '{block_label}.{tier}'")
                    break
        suggestion_str = ("; " + "; ".join(suggestions)) if suggestions else ""
        raise ConfigError(
            f"{ctx}: '{block_label}' has unknown tier keys {unknown_tiers}; "
            f"accepted tiers: {list(tier_keys)}{suggestion_str}"
        )

    if require_complete:
        missing_tiers = sorted(set(tier_keys) - set(raw))
        if missing_tiers:
            raise ConfigError(
                f"{ctx}: top-level '{block_label}' is missing required tier keys "
                f"{missing_tiers}; expected all of {list(tier_keys)}"
            )

    specs: dict[str, GateSpec] = {}
    tier_of: dict[str, str] = {}

    for tier in tier_keys:
        sub = raw.get(tier)
        valid_names = tier_names.get(tier, frozenset())
        if sub is None:
            if require_complete and valid_names:
                raise ConfigError(
                    f"{ctx}: '{block_label}.{tier}' is required and must list every "
                    f"registered name in this tier: {sorted(valid_names)}"
                )
            continue
        if not isinstance(sub, dict):
            raise ConfigError(
                f"{ctx}: '{block_label}.{tier}' must be a mapping of name -> "
                f"enabled-flag; got {type(sub).__name__}"
            )

        for name in sub:
            if name in valid_names:
                continue
            correct_tier = None
            for other_tier, other_names in tier_names.items():
                if other_tier == tier:
                    continue
                if name in other_names:
                    correct_tier = other_tier
                    break
            if correct_tier is not None:
                raise ConfigError(
                    f"{ctx}: '{block_label}.{tier}.{name}' is in the wrong tier; "
                    f"move it under '{block_label}.{correct_tier}.{name}'"
                )
            raise ConfigError(
                f"{ctx}: '{block_label}.{tier}' has unknown name {name!r}; "
                f"accepted: {sorted(valid_names)}"
            )

        if require_complete:
            missing = sorted(valid_names - set(sub))
            if missing:
                raise ConfigError(
                    f"{ctx}: '{block_label}.{tier}' must explicitly set every "
                    f"registered name; missing: {missing}"
                )

        for name, body in sub.items():
            specs[name] = GateSpec(enabled=_parse_bool_or_enabled(
                body, name=f"{block_label}.{tier}.{name}", ctx=ctx,
            ))
            tier_of[name] = tier

    return Gates(specs=specs, tier_of=tier_of)


def _format_tier_blueprint(
    tier_keys: tuple[str, ...], tier_names: dict[str, frozenset[str]],
) -> str:
    lines = []
    for tier in tier_keys:
        names = sorted(tier_names.get(tier, set()))
        lines.append(f"  {tier}:")
        for n in names:
            lines.append(f"    {n}: true|false")
    return "\n".join(lines)


def _parse_bool_or_enabled(raw: Any, *, name: str, ctx: str) -> bool:
    """Parse a leaf entry: either a bool or `{enabled: bool}`."""
    if isinstance(raw, bool):
        return raw
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{ctx}: '{name}' must be a boolean or a mapping with 'enabled'; "
            f"got {type(raw).__name__}. Example: `{name}: true` or "
            f"`{name}: {{ enabled: true }}`."
        )
    allowed = {"enabled"}
    extras = sorted(set(raw) - allowed)
    if extras:
        raise ConfigError(
            f"{ctx}: '{name}' has unknown keys {extras}; accepted: {sorted(allowed)}"
        )
    if "enabled" not in raw:
        raise ConfigError(
            f"{ctx}: '{name}' is missing required key 'enabled' (true | false)"
        )
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{ctx}: '{name}.enabled' must be a boolean (true | false); got {enabled!r}"
        )
    return enabled
