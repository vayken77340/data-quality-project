"""Validation config loader.

YAML shape:

    contracts_folder: "epics/1118/contracts"   # optional; defaults to <epic_dir>/contracts
    defaults:                                  # optional; inherited by tables without overrides
      format: excel
      file_pattern: "data.xlsx"
      parser_overrides:                        # shallow-merged with per-table overrides
        header_row: 1

    # Omit `tables` to validate every <table>.yaml under contracts_folder using `defaults`.
    # Listing tables here scopes validation to that subset; per-table blocks may
    # override defaults and add table-only fields (sheet_name, field_mapping).
    tables:
      PROJECT:                                 # empty value = defaults apply, no overrides
      CALENDAR:
        sheet_name: "Calendar Sheet"

    settings:                                  # optional
      extra_columns_severity: warning
      rejected_row_cap: 500

Layer interaction:
- `parsers/<format>.yaml` provides parser defaults (encoding, delimiter, ...).
- `defaults.parser_overrides` overrides those.
- `tables.<T>.parser_overrides` further overrides defaults per-table.
- `tables.<T>.sheet_name` flows in as a final override into the merged params.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError
from data_contract.validate_data.parsers import get_by_name


@dataclass(frozen=True)
class CheckSpec:
    """Per-check spec carried in a CheckGates entry.

    `enabled` is the on/off toggle. Descriptions used to live here too; they
    now live globally in `configs/report_strings.yaml` under
    `checks_sheet.descriptions` so each epic's validation.yaml doesn't have
    to duplicate them.
    """
    enabled: bool


@dataclass(frozen=True)
class CheckGates:
    """Per-check spec map.

    `specs[name]` carries the CheckSpec for each check. A child overlay
    (per-table) is merged on top of the parent (global) via `with_overrides`:
    per-table beats global; absent keys inherit.

    `is_enabled(name)` returns True if absent (defensive); production loads go
    through `_parse_check_gates(require_complete=True)` which guarantees every
    check is explicitly set, so this fallback only matters for unit tests.
    """
    specs: dict[str, CheckSpec] = field(default_factory=dict)

    def is_enabled(self, name: str) -> bool:
        spec = self.specs.get(name)
        return spec.enabled if spec is not None else True

    def with_overrides(self, override: "CheckGates") -> "CheckGates":
        """Merge `override` on top of self. Per-table beats global key by key."""
        merged: dict[str, CheckSpec] = {**self.specs}
        for name, child_spec in override.specs.items():
            merged[name] = child_spec
        return CheckGates(specs=merged)

    @property
    def values(self) -> dict[str, bool]:
        """Back-compat shim: callers that just want the enabled/disabled map."""
        return {name: spec.enabled for name, spec in self.specs.items()}


@dataclass(frozen=True)
class TableValidationConfig:
    table: str
    format: str
    file_pattern: str
    parser_overrides: dict[str, Any] = field(default_factory=dict)
    field_mapping: dict[str, str] = field(default_factory=dict)
    checks: CheckGates = field(default_factory=CheckGates)


@dataclass(frozen=True)
class ValidationSettings:
    extra_columns_severity: str = "warning"
    rejected_row_cap: int = 500


_VALID_EXTRA_COLUMN_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})

# Keys allowed under `defaults`. Per-table-only fields (sheet_name, field_mapping)
# are deliberately excluded — they don't make sense as defaults.
_DEFAULTS_ALLOWED = frozenset({"format", "file_pattern", "parser_overrides"})

# Allowed check names under `checks:` blocks. Covers structural / per-field core
# checks AND per-constraint checks (the latter match constraint registry names).
# Listed here statically so config typos fail at load rather than silently being
# treated as "enabled".
_VALID_CHECK_NAMES = frozenset({
    # Structural / per-field core
    "type_coercion", "boolean_coercion", "nullable", "max_length",
    "column_missing",
    # Keys
    "pk_uniqueness", "fk_existence",
    # Per-constraint (must match FieldConstraint.name)
    "allowed_values", "pattern", "min_value", "max_value", "format", "unique",
})


def _parse_check_gates(
    raw: Any, *, ctx: str, require_complete: bool = False,
) -> CheckGates:
    """Parse a `checks:` block into a CheckGates.

    Each entry is either a bare boolean (shorthand) or a mapping that
    carries `enabled: <bool>`:

        checks:
          type_coercion: true              # shorthand
          boolean_coercion:                # dict form
            enabled: true
          ...

    Descriptions are NOT carried here -- they live globally in
    `configs/report_strings.yaml` under `checks_sheet.descriptions`.

    `require_complete=True` enforces that EVERY valid check name is explicitly
    present. Used for the top-level block in validation.yaml.

    `require_complete=False` allows partial entries (per-table override blocks).
    """
    if raw is None:
        if require_complete:
            raise ConfigError(
                f"{ctx}: top-level 'checks' block is required and must explicitly "
                f"enable or disable every check. Add a block listing all of: "
                f"{sorted(_VALID_CHECK_NAMES)} (each as `<name>: true|false`)."
            )
        return CheckGates()
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: 'checks' must be a mapping of check_name -> enabled-flag")
    unknown = sorted(set(raw) - _VALID_CHECK_NAMES)
    if unknown:
        raise ConfigError(
            f"{ctx}: 'checks' has unknown check names {unknown}; "
            f"accepted: {sorted(_VALID_CHECK_NAMES)}"
        )
    if require_complete:
        missing = sorted(_VALID_CHECK_NAMES - set(raw))
        if missing:
            raise ConfigError(
                f"{ctx}: top-level 'checks' block must explicitly set every check; "
                f"missing: {missing}."
            )
    specs: dict[str, CheckSpec] = {}
    for name, body in raw.items():
        specs[name] = _parse_check_spec(body, name=name, ctx=ctx)
    return CheckGates(specs=specs)


def _parse_check_spec(raw: Any, *, name: str, ctx: str) -> CheckSpec:
    """Parse one `checks.<name>` entry: either a bool or `{enabled: bool}`."""
    if isinstance(raw, bool):
        return CheckSpec(enabled=raw)
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{ctx}: 'checks.{name}' must be a boolean or a mapping with 'enabled'; "
            f"got {type(raw).__name__}. Example: `{name}: true` or "
            f"`{name}: {{ enabled: true }}`."
        )
    allowed = {"enabled"}
    extras = sorted(set(raw) - allowed)
    if extras:
        raise ConfigError(
            f"{ctx}: 'checks.{name}' has unknown keys {extras}; accepted: {sorted(allowed)}. "
            f"Descriptions live in configs/report_strings.yaml under "
            f"`checks_sheet.descriptions`, not validation.yaml."
        )
    if "enabled" not in raw:
        raise ConfigError(
            f"{ctx}: 'checks.{name}' is missing required key 'enabled' (true | false)"
        )
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{ctx}: 'checks.{name}.enabled' must be a boolean (true | false); got {enabled!r}"
        )
    return CheckSpec(enabled=enabled)


@dataclass(frozen=True)
class _DefaultsBlock:
    format: str | None = None
    file_pattern: str | None = None
    parser_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationConfig:
    settings: ValidationSettings
    parser_yaml_dir: Path
    contracts_folder: Path | None = None        # None => runner falls back to <epic_dir>/contracts
    defaults: _DefaultsBlock = field(default_factory=_DefaultsBlock)
    # `tables` resolved per the rules. Empty dict => validate every contract found in the folder.
    tables: dict[str, TableValidationConfig] = field(default_factory=dict)
    # Names listed under `tables:` in YAML, preserved so the runner can detect
    # "table listed in filter but no matching contract" and emit the right error.
    declared_table_filter: tuple[str, ...] = ()
    # Required database target. The runner loads the target YAML from
    # configs/targets/<target>.yaml (or epics/<E>/configs/targets/<t>.yaml)
    # and overlays it on the base TypeRegistry so per-target rules (bounds,
    # boolean tokens, length-units, physical_type) drive validation. There is
    # no "no target" mode: universal logical types only acquire concrete
    # validation rules once mapped to a target's physical types.
    target: str = ""
    # Global enable/disable gates for individual checks. Per-table `checks:`
    # blocks in `tables.<T>.checks` override these via `with_overrides`.
    checks: CheckGates = field(default_factory=CheckGates)

    @classmethod
    def from_yaml(cls, validation_yaml: Path, parser_yaml_dir: Path) -> "ValidationConfig":
        if not validation_yaml.is_file():
            raise ConfigError(f"validation config not found: {validation_yaml}")
        raw = yaml.safe_load(validation_yaml.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{validation_yaml}: top-level YAML must be a mapping")

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
        # The contract uses universal logical types (string, int32, etc.) -- these
        # have no inherent ranges or formats until grounded in a target database's
        # physical types. Validation is target-specific by design: there is no
        # such thing as "default type validation", so the target MUST be declared
        # explicitly in validation.yaml.
        target_raw = raw.get("target")
        if target_raw is None:
            raise ConfigError(
                f"{validation_yaml}: 'target' is required. Declare which target "
                f"database the data is validated against -- e.g. `target: postgres` "
                f"at the top level. Universal contract types only acquire concrete "
                f"validation rules once mapped to a target's physical types."
            )
        if not isinstance(target_raw, str) or not target_raw:
            raise ConfigError(
                f"{validation_yaml}: 'target' must be a non-empty string"
            )
        target: str = target_raw

        # --- checks (global) ---------------------------------------------------
        # The global `checks:` block is REQUIRED and must list every check
        # name explicitly. Per-table `checks:` blocks (parsed in _resolve_table)
        # are optional and may list only the keys that differ from global.
        global_checks = _parse_check_gates(
            raw.get("checks"), ctx=str(validation_yaml), require_complete=True,
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
            parser_yaml_dir=parser_yaml_dir,
            contracts_folder=contracts_folder,
            defaults=defaults_block,
            tables=tables,
            declared_table_filter=tuple(declared_filter),
            target=target,
            checks=global_checks,
        )

    def is_filtered(self) -> bool:
        """True when the YAML declared an explicit `tables:` filter."""
        return bool(self.declared_table_filter)

    def build_table_entry(self, table_name: str) -> TableValidationConfig:
        """Build a TableValidationConfig for a discovered-but-unlisted table,
        using the defaults block. Used when `tables:` is omitted entirely.
        """
        return _resolve_table(
            table_name=table_name,
            table_raw={},
            defaults=self.defaults,
            validation_yaml=Path("<discovered>"),
            global_checks=self.checks,
        )

    def effective_parser_params(self, table_cfg: TableValidationConfig) -> dict[str, Any]:
        """Merge `parsers/<format>.yaml` + defaults.parser_overrides + table.parser_overrides.

        Later layers win. Format-specific params (sheet_name, encoding, etc.)
        live exclusively in parser_overrides — there's no longer a top-level
        format-specific field on TableValidationConfig.
        """
        parser_yaml = self.parser_yaml_dir / f"{table_cfg.format}.yaml"
        parser_yaml_defaults: dict[str, Any] = {}
        if parser_yaml.is_file():
            loaded = yaml.safe_load(parser_yaml.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{parser_yaml}: top-level YAML must be a mapping")
            parser_yaml_defaults = loaded
        return {**parser_yaml_defaults, **self.defaults.parser_overrides, **table_cfg.parser_overrides}


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
        get_by_name(fmt)  # validate registered
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
) -> TableValidationConfig:
    # --- format -----------------------------------------------------------
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

    # --- file_pattern -----------------------------------------------------
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

    # --- parser_overrides (table layer; defaults layer is merged later) ---
    table_overrides_raw = table_raw.get("parser_overrides", {}) or {}
    if not isinstance(table_overrides_raw, dict):
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.parser_overrides must be a mapping"
        )
    # Validate the merged set against the parser's allowlist now so typos fail at load.
    merged_keys = set(defaults.parser_overrides) | set(table_overrides_raw)
    unknown = sorted(merged_keys - set(parser_cls.PARSER_PARAMS))
    if unknown:
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name} parser_overrides include unknown keys "
            f"{unknown}; accepted for {fmt!r}: {list(parser_cls.PARSER_PARAMS)}"
        )

    # --- field_mapping (per-table only) -----------------------------------
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

    # Reject misplaced format-specific keys at the table top level. Forces
    # users to put parser-side params (sheet_name, encoding, delimiter, etc.)
    # under parser_overrides where the format's PARSER_PARAMS allowlist gates
    # them. Keeps the table block format-agnostic.
    for misplaced in ("sheet_name", "encoding", "delimiter", "header_row", "null_tokens", "quote_char"):
        if misplaced in table_raw:
            raise ConfigError(
                f"{validation_yaml}: tables.{table_name}.{misplaced} is a parser param; "
                f"move it under tables.{table_name}.parser_overrides"
            )

    # --- checks (per-table override of global) ----------------------------
    table_checks = _parse_check_gates(
        table_raw.get("checks"),
        ctx=f"{validation_yaml}: tables.{table_name}",
    )
    effective_checks = global_checks.with_overrides(table_checks)

    return TableValidationConfig(
        table=table_name,
        format=fmt,
        file_pattern=file_pattern,
        parser_overrides=dict(table_overrides_raw),
        field_mapping=dict(mapping_raw),
        checks=effective_checks,
    )
