"""Validation config loader.

YAML shape (top level):

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
        format: csv                            # overrides defaults.format
        file_pattern: "calendar*.csv"          # overrides defaults.file_pattern
        parser_overrides:                      # per-format parser params,
          field_matching_policy: similarity    #   allowlist enforced per parser
        checks:                                # partial override allowed
          field:
            unique: true
        metrics:
          field:
            duplicate_pct: false

Per-table keys recognized under `tables.<T>:`:

  format            -- parser name; overrides defaults.format.
  file_pattern      -- glob; overrides defaults.file_pattern.
  parser_overrides  -- per-format parser params (allowlist enforced by
                       each parser's PARSER_PARAMS). The generic
                       `field_matching_policy` ("positional" | "exact" |
                       "similarity") is added automatically by the
                       FileParser base; the global similarity_threshold
                       comes from .env.
  checks            -- per-tier partial overrides on the global gates.
  metrics           -- same shape as checks.

`target:` and `field_mapping:` were removed. Target lives on each
generated contract (the runner reads it from the loaded contracts).
Per-field source-vs-database renames live on the contract as well:
each `FieldContract` carries `source_name` (the raw spec header) and
`name` (the database identifier). Spec authors override the auto-slug
via a `Nom BDD` cell in the spec when needed.

Operator toggles (`rejected_row_cap`, `extra_columns_severity`,
`similarity_threshold`) live in `.env` so they can vary by environment
(CI vs dev) without editing this YAML. See `settings.py`.

Column-binding strategy
-----------------------
Owned by each parser via the generic `field_matching_policy` param
(in `parser_overrides`):
  * `positional` (CSV / Excel default) -- the i-th data column is the
    i-th contract field. Header text is ignored. Rename happens per
    file inside `data_parsers.field_matching.apply_policy`, before
    multi-file concat, so CSVs with disagreeing headers still align.
  * `exact` (JSON default) -- columns whose names match a contract
    field's `source_name` (preferred) or `name` bind by string equality.
    Mismatched columns surface via `column_missing` / `extra_column`.
  * `similarity` -- fuzzy match against `source_name` (preferred) /
    `name` with the global `similarity_threshold` (.env). Catches
    typos, case, spacing, punctuation, and word-order variants.

`checks.structural.field_names_from_sample` and
`checks.structural.field_types_from_sample` are pure DRIFT checks: each
parser still records the file's original header text in its
`ParserSchema`, and the runner compares that against the contract when
either gate is enabled. They do NOT drive parsing.

Tier-keyed parsing lives in `core/gates.py`. This module wires the
validation-specific tier definitions (which names belong to which
tier) on top of that shared parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data_contract.core.gates import Gates, GateSpec, parse_tier_gates
from data_contract.core.yaml_io import load_yaml_mapping
from data_contract.data_parsers import get_by_name, load_parser_yaml_overrides
from data_contract.errors import ConfigError


# Structural checks are derived from the contract type system; the set is
# hard-coded (a "new structural check" really means a new Type in
# configs/types.yaml plus a phase in `_validate_one_table`).
_STRUCTURAL_CHECK_NAMES: frozenset[str] = frozenset({
    "type_coercion", "boolean_coercion", "nullable", "max_length",
    # Source-schema drift checks (default off). The parser may expose a
    # ParserSchema with column_names / column_types; these checks compare
    # that against the contract and emit warnings on mismatch.
    "field_names_from_sample", "field_types_from_sample",
})

CHECK_TIER_KEYS: tuple[str, ...] = ("structural", "table", "field")
METRIC_TIER_KEYS: tuple[str, ...] = ("field", "table")


def _tier_check_names() -> dict[str, frozenset[str]]:
    """Per-tier set of valid check names.

    Pulled from the two registries at call time so the parser auto-picks
    up newly registered plugins. Structural is hard-coded.

    Field-tier names are filtered to constraints that actually emit a
    violation; contract-only constraints (default_value) have no data-side
    check and shouldn't appear as a togglable gate.
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


def _parse_check_gates(raw: Any, *, ctx: str, require_complete: bool = False) -> Gates:
    return parse_tier_gates(
        raw, ctx=ctx, block_label="checks",
        tier_keys=CHECK_TIER_KEYS,
        tier_names=_tier_check_names(),
        require_complete=require_complete,
    )


def _parse_metric_gates(raw: Any, *, ctx: str, require_complete: bool = False) -> Gates:
    return parse_tier_gates(
        raw, ctx=ctx, block_label="metrics",
        tier_keys=METRIC_TIER_KEYS,
        tier_names=_tier_metric_names(),
        require_complete=require_complete,
    )


# ---------------------------------------------------------------------------
# Top-level dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableValidationConfig:
    table: str
    format: str
    file_pattern: str
    parser_overrides: dict[str, Any] = field(default_factory=dict)
    checks: Gates = field(default_factory=Gates)
    metrics: Gates = field(default_factory=Gates)


_DEFAULTS_ALLOWED = frozenset({"format", "file_pattern", "parser_overrides"})


@dataclass(frozen=True)
class _DefaultsBlock:
    format: str | None = None
    file_pattern: str | None = None
    parser_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationConfig:
    parser_yaml_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    contracts_folder: Path | None = None
    defaults: _DefaultsBlock = field(default_factory=_DefaultsBlock)
    tables: dict[str, TableValidationConfig] = field(default_factory=dict)
    declared_table_filter: tuple[str, ...] = ()
    checks: Gates = field(default_factory=Gates)
    metrics: Gates = field(default_factory=Gates)

    @classmethod
    def from_yaml(
        cls, validation_yaml: Path, parsers_yaml_path: Path,
    ) -> "ValidationConfig":
        if not validation_yaml.is_file():
            raise ConfigError(f"validation config not found: {validation_yaml}")
        raw = load_yaml_mapping(validation_yaml, what="validation config")

        parser_yaml_overrides = load_parser_yaml_overrides(parsers_yaml_path)

        contracts_folder_raw = raw.get("contracts_folder")
        contracts_folder: Path | None = None
        if contracts_folder_raw is not None:
            if not isinstance(contracts_folder_raw, str) or not contracts_folder_raw:
                raise ConfigError(
                    f"{validation_yaml}: 'contracts_folder', if set, must be a non-empty string"
                )
            contracts_folder = Path(contracts_folder_raw)

        if "target" in raw:
            raise ConfigError(
                f"{validation_yaml}: the 'target:' field was moved to the epic "
                f"version config (e.g. configs/contracts/v1.0.yaml) and is now stamped "
                f"onto each generated contract. Remove 'target:' from "
                f"validation.yaml; the runner reads it from the contracts."
            )

        global_checks = _parse_check_gates(
            raw.get("checks"), ctx=str(validation_yaml), require_complete=True,
        )
        global_metrics = _parse_metric_gates(
            raw.get("metrics"), ctx=str(validation_yaml), require_complete=True,
        )

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
            tables[table_name] = _resolve_table(
                table_name=table_name,
                table_raw=table_raw or {},
                defaults=defaults_block,
                validation_yaml=validation_yaml,
                global_checks=global_checks,
                global_metrics=global_metrics,
            )

        # `settings:` used to live here but moved to .env (see settings.py).
        # Reject the block so a stale config fails loudly instead of silently
        # being ignored when an operator forgets to migrate.
        if "settings" in raw:
            raise ConfigError(
                f"{validation_yaml}: the 'settings:' block was removed -- move "
                f"'rejected_row_cap' and 'extra_columns_severity' to .env "
                f"(top-level keys). See settings.py for the supported variables."
            )

        return cls(
            parser_yaml_overrides=parser_yaml_overrides,
            contracts_folder=contracts_folder,
            defaults=defaults_block,
            tables=tables,
            declared_table_filter=tuple(declared_filter),
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
    global_checks: Gates = Gates(),
    global_metrics: Gates = Gates(),
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
    merged = {**defaults.parser_overrides, **table_overrides_raw}
    parser_cls.validate_params(
        merged, ctx=f"{validation_yaml}: tables.{table_name} parser_overrides",
    )

    if "field_mapping" in table_raw:
        raise ConfigError(
            f"{validation_yaml}: tables.{table_name}.field_mapping was removed. "
            f"Per-field renames now live on the contract: each FieldContract "
            f"carries `source_name` (the raw header) and `name` (the database "
            f"identifier). Declare an explicit `Nom BDD` cell in the spec when "
            f"the auto-slugified database name needs an override."
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
        checks=effective_checks,
        metrics=effective_metrics,
    )
