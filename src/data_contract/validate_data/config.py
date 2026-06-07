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
class TableValidationConfig:
    table: str
    format: str
    file_pattern: str
    parser_overrides: dict[str, Any] = field(default_factory=dict)
    field_mapping: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationSettings:
    extra_columns_severity: str = "warning"
    rejected_row_cap: int = 500


_VALID_EXTRA_COLUMN_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})

# Keys allowed under `defaults`. Per-table-only fields (sheet_name, field_mapping)
# are deliberately excluded — they don't make sense as defaults.
_DEFAULTS_ALLOWED = frozenset({"format", "file_pattern", "parser_overrides"})


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

    return TableValidationConfig(
        table=table_name,
        format=fmt,
        file_pattern=file_pattern,
        parser_overrides=dict(table_overrides_raw),
        field_mapping=dict(mapping_raw),
    )
