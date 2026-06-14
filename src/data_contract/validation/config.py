"""Validation config loader.

YAML shape (top level):

    target: oracle
    contracts_folder: "epics/1118/contracts"   # optional
    defaults:                                  # optional
      format: excel
      file_pattern: "data.xlsx"
      parser_overrides:
        header_row: 1

    checks:                                    # required; tier-keyed
      structural:                              #   type-system tier (hard-coded set)
        type_coercion: true
        boolean_coercion: true
        nullable: true
        max_length: true
      table:                                   #   table_checks/ registry
        column_missing: true
        pk_uniqueness: true
        fk_existence: false
      field:                                   #   field_constraints/ registry
        allowed_values: false
        pattern: false
        min_value: false
        max_value: false
        format: false
        unique: false

    metrics:                                   # required; tier-keyed by scope
      field:
        null_count: true
        null_percentage: true
        distinct_count: true
        completeness: true
        duplicate_pct: true
      table:
        row_count: true

    tables:                                    # optional
      PROJECT:
      CALENDAR:
        checks:                                # partial override allowed
          field:
            unique: true
        metrics:
          field:
            duplicate_pct: false

    settings:                                  # optional
      extra_columns_severity: warning
      rejected_row_cap: 500

Layer interaction:
- `parsers/<format>.yaml` provides parser defaults (encoding, delimiter, ...).
- `defaults.parser_overrides` overrides those.
- `tables.<T>.parser_overrides` further overrides defaults per-table.

Tier-keyed parsing:
- Each tier sub-block must be exhaustive at the top level (lists every
  name registered in that tier).
- Per-table overrides accept partial sub-blocks; placing a name in the
  wrong tier raises a "wrong tier" ConfigError.
- Internally, gates are FLATTENED into a single dict[name -> spec] plus
  a tier_of[name -> tier] index. The runner only consults `is_enabled`,
  unaware of the tier layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError
from data_contract.data_parsers import get_by_name


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

# Structural checks are derived from the contract type system; the set is
# hard-coded (a "new structural check" really means a new Type in
# configs/types.yaml plus a phase in `_validate_one_table`).
_STRUCTURAL_CHECK_NAMES: frozenset[str] = frozenset({
    "type_coercion", "boolean_coercion", "nullable", "max_length",
    # Source-schema drift checks (default off). The parser may expose a
    # ParserSchema with column_names / column_types; these checks compare
    # that against the contract and emit warnings on mismatch. Skipped
    # silently when the parser doesn't report a schema (e.g. fixed-width).
    "field_names_from_sample", "field_types_from_sample",
})

CHECK_TIER_KEYS: tuple[str, ...] = ("structural", "table", "field")
METRIC_TIER_KEYS: tuple[str, ...] = ("field", "table")


def _tier_check_names() -> dict[str, frozenset[str]]:
    """Per-tier set of valid check names.

    Pulled from the two registries at call time so the parser auto-picks
    up newly registered plugins. Structural is hard-coded.

    Field-tier names are filtered to constraints that actually emit a
    violation (i.e. override `check_data`, signalled by a non-empty
    VIOLATION_KIND); contract-only constraints like `default_value` have
    no data-side check and shouldn't appear as a togglable gate.
    """
    from data_contract import field_constraints as fc_pkg
    from data_contract import table_checks as tc_pkg
    field_names = {
        name for name, cls in fc_pkg.REGISTRY.items()
        if isinstance(cls.VIOLATION_KIND, str) and cls.VIOLATION_KIND
    }
    return {
        "structural": _STRUCTURAL_CHECK_NAMES,
        "table": frozenset(tc_pkg.REGISTRY),
        "field": frozenset(field_names),
    }


def _tier_metric_names() -> dict[str, frozenset[str]]:
    """Per-tier set of valid metric names, grouped by each metric's `scope`."""
    from data_contract import metrics as metrics_pkg
    out: dict[str, set[str]] = {tier: set() for tier in METRIC_TIER_KEYS}
    for name, cls in metrics_pkg.REGISTRY.items():
        if cls.scope in out:
            out[cls.scope].add(name)
    return {tier: frozenset(names) for tier, names in out.items()}


# ---------------------------------------------------------------------------
# Spec / gates dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckSpec:
    """Per-check spec carried in a CheckGates entry.

    `enabled` is the on/off toggle. Descriptions live globally in
    `configs/report_strings.yaml`.
    """
    enabled: bool


@dataclass(frozen=True)
class CheckGates:
    """Flat per-check spec map plus a tier index.

    `specs[name]` carries the CheckSpec for each check; `tier_of[name]`
    records which tier the entry came from (`"structural"`, `"table"`,
    `"field"`), used for "wrong tier" error messages on per-table
    overrides. The runner only reads `specs` via `is_enabled(name)`.
    """
    specs: dict[str, CheckSpec] = field(default_factory=dict)
    tier_of: dict[str, str] = field(default_factory=dict)

    def is_enabled(self, name: str) -> bool:
        spec = self.specs.get(name)
        return spec.enabled if spec is not None else True

    def with_overrides(self, override: "CheckGates") -> "CheckGates":
        """Merge `override` on top of self. Per-table beats global key by key."""
        merged_specs: dict[str, CheckSpec] = {**self.specs}
        merged_tier: dict[str, str] = {**self.tier_of}
        for name, child_spec in override.specs.items():
            merged_specs[name] = child_spec
        for name, tier in override.tier_of.items():
            merged_tier[name] = tier
        return CheckGates(specs=merged_specs, tier_of=merged_tier)

    @property
    def values(self) -> dict[str, bool]:
        return {name: spec.enabled for name, spec in self.specs.items()}


@dataclass(frozen=True)
class MetricSpec:
    enabled: bool


@dataclass(frozen=True)
class MetricGates:
    specs: dict[str, MetricSpec] = field(default_factory=dict)
    tier_of: dict[str, str] = field(default_factory=dict)

    def is_enabled(self, name: str) -> bool:
        spec = self.specs.get(name)
        return spec.enabled if spec is not None else True

    def with_overrides(self, override: "MetricGates") -> "MetricGates":
        merged_specs: dict[str, MetricSpec] = {**self.specs}
        merged_tier: dict[str, str] = {**self.tier_of}
        for name, child_spec in override.specs.items():
            merged_specs[name] = child_spec
        for name, tier in override.tier_of.items():
            merged_tier[name] = tier
        return MetricGates(specs=merged_specs, tier_of=merged_tier)

    @property
    def values(self) -> dict[str, bool]:
        return {name: spec.enabled for name, spec in self.specs.items()}


@dataclass(frozen=True)
class TableValidationConfig:
    table: str
    format: str
    file_pattern: str
    parser_overrides: dict[str, Any] = field(default_factory=dict)
    field_mapping: dict[str, str] = field(default_factory=dict)
    checks: CheckGates = field(default_factory=CheckGates)
    metrics: MetricGates = field(default_factory=MetricGates)


@dataclass(frozen=True)
class ValidationSettings:
    extra_columns_severity: str = "warning"
    rejected_row_cap: int = 500


_VALID_EXTRA_COLUMN_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})

_DEFAULTS_ALLOWED = frozenset({"format", "file_pattern", "parser_overrides"})


# ---------------------------------------------------------------------------
# Tier-keyed parsing (shared between checks and metrics)
# ---------------------------------------------------------------------------


def _parse_tier_gates(
    raw: Any,
    *,
    ctx: str,
    block_label: str,                    # "checks" or "metrics"
    tier_keys: tuple[str, ...],          # CHECK_TIER_KEYS or METRIC_TIER_KEYS
    tier_names: dict[str, frozenset[str]],
    require_complete: bool,
) -> tuple[dict[str, bool], dict[str, str]]:
    """Parse a tier-keyed block and return (flat enabled-by-name, tier_of).

    Top-level `require_complete=True`:
      * every tier key must be present
      * within each tier every registered name must be present
    Per-table override `require_complete=False`:
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
        return {}, {}

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

    flat_enabled: dict[str, bool] = {}
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

        # Detect names placed in the wrong tier and report the right one.
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
            flat_enabled[name] = _parse_bool_or_enabled(
                body, name=f"{block_label}.{tier}.{name}", ctx=ctx,
            )
            tier_of[name] = tier

    return flat_enabled, tier_of


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


def _parse_check_gates(
    raw: Any, *, ctx: str, require_complete: bool = False,
) -> CheckGates:
    flat, tier_of = _parse_tier_gates(
        raw,
        ctx=ctx,
        block_label="checks",
        tier_keys=CHECK_TIER_KEYS,
        tier_names=_tier_check_names(),
        require_complete=require_complete,
    )
    return CheckGates(
        specs={name: CheckSpec(enabled=enabled) for name, enabled in flat.items()},
        tier_of=tier_of,
    )


def _parse_metric_gates(
    raw: Any, *, ctx: str, require_complete: bool = False,
) -> MetricGates:
    flat, tier_of = _parse_tier_gates(
        raw,
        ctx=ctx,
        block_label="metrics",
        tier_keys=METRIC_TIER_KEYS,
        tier_names=_tier_metric_names(),
        require_complete=require_complete,
    )
    return MetricGates(
        specs={name: MetricSpec(enabled=enabled) for name, enabled in flat.items()},
        tier_of=tier_of,
    )


@dataclass(frozen=True)
class _DefaultsBlock:
    format: str | None = None
    file_pattern: str | None = None
    parser_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationConfig:
    settings: ValidationSettings
    # Per-format parser defaults loaded once from `configs/parsers.yaml`.
    # Empty dict when the file is missing -- the runner then falls through
    # to validation.yaml overrides for every value. Stored already-parsed
    # so the validation runner doesn't redo filesystem work per table.
    parser_yaml_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    contracts_folder: Path | None = None
    defaults: _DefaultsBlock = field(default_factory=_DefaultsBlock)
    tables: dict[str, TableValidationConfig] = field(default_factory=dict)
    declared_table_filter: tuple[str, ...] = ()
    target: str = ""
    checks: CheckGates = field(default_factory=CheckGates)
    metrics: MetricGates = field(default_factory=MetricGates)

    @classmethod
    def from_yaml(
        cls, validation_yaml: Path, parsers_yaml_path: Path,
    ) -> "ValidationConfig":
        if not validation_yaml.is_file():
            raise ConfigError(f"validation config not found: {validation_yaml}")
        raw = yaml.safe_load(validation_yaml.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{validation_yaml}: top-level YAML must be a mapping")

        # --- per-format parser defaults (configs/parsers.yaml) ----------------
        # Load once; the dict is then merged with validation.yaml-side overrides
        # at `effective_parser_params` call time.
        from data_contract.data_parsers import load_parser_yaml_overrides
        parser_yaml_overrides = load_parser_yaml_overrides(parsers_yaml_path)

        # --- contracts_folder --------------------------------------------------
        contracts_folder_raw = raw.get("contracts_folder")
        contracts_folder: Path | None = None
        if contracts_folder_raw is not None:
            if not isinstance(contracts_folder_raw, str) or not contracts_folder_raw:
                raise ConfigError(
                    f"{validation_yaml}: 'contracts_folder', if set, must be a non-empty string"
                )
            contracts_folder = Path(contracts_folder_raw)

        # --- target ------------------------------------------------------------
        target_raw = raw.get("target")
        if target_raw is None:
            raise ConfigError(
                f"{validation_yaml}: 'target' is required. Declare which target "
                f"database the data is validated against -- e.g. `target: postgres` "
                f"at the top level."
            )
        if not isinstance(target_raw, str) or not target_raw:
            raise ConfigError(
                f"{validation_yaml}: 'target' must be a non-empty string"
            )
        target: str = target_raw

        # --- checks (global, tier-keyed, exhaustive) --------------------------
        global_checks = _parse_check_gates(
            raw.get("checks"), ctx=str(validation_yaml), require_complete=True,
        )

        # --- metrics (global, tier-keyed, exhaustive) -------------------------
        global_metrics = _parse_metric_gates(
            raw.get("metrics"), ctx=str(validation_yaml), require_complete=True,
        )

        # --- defaults block ---------------------------------------------------
        defaults_raw = raw.get("defaults", {}) or {}
        if not isinstance(defaults_raw, dict):
            raise ConfigError(f"{validation_yaml}: 'defaults' must be a mapping")
        unknown_defaults = sorted(set(defaults_raw) - _DEFAULTS_ALLOWED)
        if unknown_defaults:
            raise ConfigError(
                f"{validation_yaml}: 'defaults' has unknown keys {unknown_defaults}; "
                f"accepted: {sorted(_DEFAULTS_ALLOWED)}"
            )
        defaults_block = _parse_defaults_block(defaults_raw, validation_yaml)

        # --- tables block (optional) ------------------------------------------
        tables_raw = raw.get("tables")
        if tables_raw is None:
            tables_block: dict[str, dict | None] = {}
        elif isinstance(tables_raw, dict):
            tables_block = dict(tables_raw)
        else:
            raise ConfigError(f"{validation_yaml}: 'tables', if set, must be a mapping")

        tables: dict[str, TableValidationConfig] = {}
        declared_filter: list[str] = []
        for table_name, table_raw in tables_block.items():
            declared_filter.append(table_name)
            entry = _resolve_table(
                table_name=table_name,
                table_raw=table_raw or {},
                defaults=defaults_block,
                validation_yaml=validation_yaml,
                global_checks=global_checks,
                global_metrics=global_metrics,
            )
            tables[table_name] = entry

        # --- settings ---------------------------------------------------------
        settings_raw = raw.get("settings", {}) or {}
        if not isinstance(settings_raw, dict):
            raise ConfigError(f"{validation_yaml}: 'settings' must be a mapping")
        extra_sev = settings_raw.get("extra_columns_severity", "warning")
        if extra_sev not in _VALID_EXTRA_COLUMN_SEVERITIES:
            raise ConfigError(
                f"{validation_yaml}: settings.extra_columns_severity must be one of "
                f"{sorted(_VALID_EXTRA_COLUMN_SEVERITIES)}; got {extra_sev!r}"
            )
        cap = int(settings_raw.get("rejected_row_cap", 500))
        if cap < 1:
            raise ConfigError(f"{validation_yaml}: settings.rejected_row_cap must be >= 1")

        return cls(
            settings=ValidationSettings(extra_columns_severity=extra_sev, rejected_row_cap=cap),
            parser_yaml_overrides=parser_yaml_overrides,
            contracts_folder=contracts_folder,
            defaults=defaults_block,
            tables=tables,
            declared_table_filter=tuple(declared_filter),
            target=target,
            checks=global_checks,
            metrics=global_metrics,
        )

    def is_filtered(self) -> bool:
        return bool(self.declared_table_filter)

    def build_table_entry(self, table_name: str) -> TableValidationConfig:
        return _resolve_table(
            table_name=table_name,
            table_raw={},
            defaults=self.defaults,
            validation_yaml=Path("<discovered>"),
            global_checks=self.checks,
            global_metrics=self.metrics,
        )

    def effective_parser_params(self, table_cfg: TableValidationConfig) -> dict[str, Any]:
        """Three-layer merge (last wins):
        1. `configs/parsers.yaml` `<format>:` block (per-format defaults).
        2. `validation.yaml: defaults.parser_overrides:` (per-run global).
        3. `validation.yaml: tables.<T>.parser_overrides:` (per-table).
        """
        return {
            **self.parser_yaml_overrides.get(table_cfg.format, {}),
            **self.defaults.parser_overrides,
            **table_cfg.parser_overrides,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_defaults_block(raw: dict, validation_yaml: Path) -> _DefaultsBlock:
    fmt = raw.get("format")
    if fmt is not None:
        if not isinstance(fmt, str) or not fmt:
            raise ConfigError(
                f"{validation_yaml}: defaults.format, if set, must be a non-empty string"
            )
        get_by_name(fmt)
    file_pattern = raw.get("file_pattern")
    if file_pattern is not None and (not isinstance(file_pattern, str) or not file_pattern):
        raise ConfigError(
            f"{validation_yaml}: defaults.file_pattern, if set, must be a non-empty string"
        )
    overrides = raw.get("parser_overrides", {}) or {}
    if not isinstance(overrides, dict):
        raise ConfigError(f"{validation_yaml}: defaults.parser_overrides must be a mapping")
    return _DefaultsBlock(
        format=fmt,
        file_pattern=file_pattern,
        parser_overrides=dict(overrides),
    )


def _resolve_table(
    *,
    table_name: str,
    table_raw: dict,
    defaults: _DefaultsBlock,
    validation_yaml: Path,
    global_checks: CheckGates = CheckGates(),
    global_metrics: MetricGates = MetricGates(),
) -> TableValidationConfig:
    raw_fmt = table_raw.get("format")
    if raw_fmt is not None and (not isinstance(raw_fmt, str) or not raw_fmt):
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.format, if set, must be a non-empty string"
        )
    fmt = raw_fmt or defaults.format
    if not fmt:
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name} has no format and "
            f"no defaults.format is set"
        )
    parser_cls = get_by_name(fmt)

    raw_pat = table_raw.get("file_pattern")
    if raw_pat is not None and (not isinstance(raw_pat, str) or not raw_pat):
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.file_pattern, if set, must be a non-empty string"
        )
    file_pattern = raw_pat or defaults.file_pattern
    if not file_pattern:
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name} has no file_pattern and "
            f"no defaults.file_pattern is set"
        )

    table_overrides_raw = table_raw.get("parser_overrides", {}) or {}
    if not isinstance(table_overrides_raw, dict):
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.parser_overrides must be a mapping"
        )
    merged_keys = set(defaults.parser_overrides) | set(table_overrides_raw)
    unknown = sorted(merged_keys - set(parser_cls.PARSER_PARAMS))
    if unknown:
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name} parser_overrides include unknown keys "
            f"{unknown}; accepted for {fmt!r}: {list(parser_cls.PARSER_PARAMS)}"
        )

    mapping_raw = table_raw.get("field_mapping", {}) or {}
    if not isinstance(mapping_raw, dict):
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.field_mapping must be a mapping"
        )
    for k, v in mapping_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(
                f"{validation_yaml}: tables.{table_name}.field_mapping keys and values "
                f"must be strings; got {k!r}: {v!r}"
            )

    for misplaced in ("sheet_name", "encoding", "delimiter", "header_row", "null_tokens", "quote_char"):
        if misplaced in table_raw:
            raise ConfigError(
                f"{validation_yaml}: tables.{table_name}.{misplaced} is a parser param; "
                f"move it under tables.{table_name}.parser_overrides"
            )

    table_checks = _parse_check_gates(
        table_raw.get("checks"),
        ctx=f"{validation_yaml}: tables.{table_name}",
    )
    effective_checks = global_checks.with_overrides(table_checks)

    table_metrics = _parse_metric_gates(
        table_raw.get("metrics"),
        ctx=f"{validation_yaml}: tables.{table_name}",
    )
    effective_metrics = global_metrics.with_overrides(table_metrics)

    return TableValidationConfig(
        table=table_name,
        format=fmt,
        file_pattern=file_pattern,
        parser_overrides=dict(table_overrides_raw),
        field_mapping=dict(mapping_raw),
        checks=effective_checks,
        metrics=effective_metrics,
    )
