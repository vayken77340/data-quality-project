"""Validation config loader.

Two YAML layers:

1. `epics/<epic>/configs/parsers/<format>.yaml` — per-parser defaults that
   apply to every file of that format in the epic. Missing files fall back
   to the parser's class-level DEFAULTS.

2. `epics/<epic>/configs/validation.yaml` — per-table mapping of contract
   table to format + file_pattern + optional field_mapping + optional
   parser_overrides.
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
    sheet_name: str | None = None        # Excel only; if unset, the parser picks the first sheet.
    parser_overrides: dict[str, Any] = field(default_factory=dict)
    field_mapping: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationSettings:
    extra_columns_severity: str = "warning"
    rejected_row_cap: int = 500


_VALID_EXTRA_COLUMN_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})


@dataclass(frozen=True)
class ValidationConfig:
    tables: dict[str, TableValidationConfig]
    settings: ValidationSettings
    parser_yaml_dir: Path

    @classmethod
    def from_yaml(cls, validation_yaml: Path, parser_yaml_dir: Path) -> "ValidationConfig":
        if not validation_yaml.is_file():
            raise ConfigError(f"validation config not found: {validation_yaml}")
        raw = yaml.safe_load(validation_yaml.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{validation_yaml}: top-level YAML must be a mapping")

        tables_block = raw.get("tables")
        if not isinstance(tables_block, dict) or not tables_block:
            raise ConfigError(f"{validation_yaml}: 'tables' must be a non-empty mapping")

        tables: dict[str, TableValidationConfig] = {}
        for table_name, table_raw in tables_block.items():
            if not isinstance(table_raw, dict):
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name} must be a mapping"
                )
            fmt = table_raw.get("format")
            if not isinstance(fmt, str) or not fmt:
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name}.format must be a non-empty string"
                )
            # Verify the format is registered + the overrides only use accepted keys.
            parser_cls = get_by_name(fmt)
            file_pattern = table_raw.get("file_pattern")
            if not isinstance(file_pattern, str) or not file_pattern:
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name}.file_pattern must be a non-empty glob"
                )

            overrides_raw = table_raw.get("parser_overrides", {}) or {}
            if not isinstance(overrides_raw, dict):
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name}.parser_overrides must be a mapping"
                )
            unknown = sorted(set(overrides_raw) - set(parser_cls.PARSER_PARAMS))
            if unknown:
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name}.parser_overrides unknown keys "
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
                        f"{validation_yaml}: tables.{table_name}.field_mapping keys and "
                        f"values must be strings; got {k!r}: {v!r}"
                    )

            sheet_name_raw = table_raw.get("sheet_name")
            if sheet_name_raw is not None and (not isinstance(sheet_name_raw, str) or not sheet_name_raw):
                raise ConfigError(
                    f"{validation_yaml}: tables.{table_name}.sheet_name, if set, must be a non-empty string"
                )

            tables[table_name] = TableValidationConfig(
                table=table_name,
                format=fmt,
                file_pattern=file_pattern,
                sheet_name=sheet_name_raw,
                parser_overrides=dict(overrides_raw),
                field_mapping=dict(mapping_raw),
            )

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
            tables=tables,
            settings=ValidationSettings(extra_columns_severity=extra_sev, rejected_row_cap=cap),
            parser_yaml_dir=parser_yaml_dir,
        )

    def effective_parser_params(self, table_cfg: TableValidationConfig) -> dict[str, Any]:
        """Merge `parsers/<format>.yaml` (when present) with `parser_overrides`.

        The table-level `sheet_name` (if set) is layered on top — declaring it
        on a parser that doesn't accept `sheet_name` (e.g. CSV) bubbles up as a
        ConfigError via the parser's PARSER_PARAMS allowlist.

        Override keys win. Unknown keys at any layer raise ConfigError.
        """
        parser_yaml = self.parser_yaml_dir / f"{table_cfg.format}.yaml"
        defaults: dict[str, Any] = {}
        if parser_yaml.is_file():
            loaded = yaml.safe_load(parser_yaml.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{parser_yaml}: top-level YAML must be a mapping")
            defaults = loaded
        merged = {**defaults, **table_cfg.parser_overrides}
        if table_cfg.sheet_name is not None:
            merged["sheet_name"] = table_cfg.sheet_name
        return merged
