from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError
from data_contract.field_constraints import REGISTRY as CONSTRAINT_REGISTRY
from data_contract.field_constraints.base import FieldConstraint
from data_contract.nullable import NullableMapping


ALL_TABLES = "__ALL__"
DEFAULTS_FILENAME = "defaults.yaml"


@dataclass(frozen=True)
class ColumnSpec:
    """A spec-column lookup.

    `column_required` controls whether the column header must exist in the workbook
    sheet (default True). When False, a missing header is tolerated and the
    column is simply skipped — values won't be read for it.

    `value_required` controls whether every non-empty data row must have a
    non-empty value in the column (default False). When True, a blank cell
    in a non-empty row produces a `missing_mandatory` rejection.

    Logical rule enforced at YAML parse time: `column_required=False + value_required=True`
    is contradictory and raises ConfigError.
    """
    spec_name: str
    column_required: bool = True
    value_required: bool = False


def _check_required_value_required(key: str, column_required: bool, value_required: bool) -> None:
    if not column_required and value_required:
        raise ConfigError(
            f"{key}: column has `column_required: false` but `value_required: true`. "
            f"A column whose existence is optional cannot also require values per row."
        )


def _parse_column_block(prefix: str, key: str, block: dict) -> ColumnSpec:
    """Shared parsing for a `{spec_name, column_required, value_required}` block."""
    if not isinstance(block, dict):
        raise ConfigError(f"{prefix}.{key} must be a mapping with 'spec_name', 'required', 'value_required'")
    spec_name = block.get("spec_name")
    if not isinstance(spec_name, str) or not spec_name:
        raise ConfigError(f"{prefix}.{key}.spec_name must be a non-empty string")
    column_required=bool(block.get("column_required", True))
    value_required = bool(block.get("value_required", False))
    _check_required_value_required(f"{prefix}.{key}", column_required, value_required)
    return ColumnSpec(spec_name=spec_name, column_required=column_required, value_required=value_required)


_CORE_KEYS = frozenset({"name", "type", "description", "nullable", "table"})


@dataclass(frozen=True)
class ColumnMapping:
    name: ColumnSpec
    type: ColumnSpec
    description: ColumnSpec
    nullable: NullableMapping
    table: ColumnSpec | None = None
    constraints: dict[str, FieldConstraint] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ColumnMapping":
        required = {"name", "type", "description", "nullable"}
        missing = required - raw.keys()
        if missing:
            raise ConfigError(f"column_mapping missing required entries: {sorted(missing)}")

        def col(key: str) -> ColumnSpec:
            return _parse_column_block("column_mapping", key, raw.get(key))

        nullable_block = raw.get("nullable")
        if not isinstance(nullable_block, dict):
            raise ConfigError("column_mapping.nullable must be a mapping")
        nullable = NullableMapping.from_dict(nullable_block)

        table_block = raw.get("table")
        if table_block is None:
            table = None
        else:
            table = _parse_column_block("column_mapping", "table", table_block)

        constraints: dict[str, FieldConstraint] = {}
        for key, value in raw.items():
            if key in _CORE_KEYS:
                continue
            if key not in CONSTRAINT_REGISTRY:
                raise ConfigError(
                    f"column_mapping.{key}: not a core key and not a registered constraint. "
                    f"Registered constraints: {sorted(CONSTRAINT_REGISTRY)}"
                )
            if not isinstance(value, dict):
                raise ConfigError(f"column_mapping.{key} must be a mapping (constraint config)")
            constraints[key] = CONSTRAINT_REGISTRY[key].from_config(value)

        return cls(
            name=col("name"),
            type=col("type"),
            description=col("description"),
            nullable=nullable,
            table=table,
            constraints=constraints,
        )


@dataclass(frozen=True)
class SplitColumnSpec:
    """A column whose cell contains a list of items joined by a separator.

    `column_required` / `value_required` mirror ColumnSpec semantics.

    FK violation tolerance is no longer carried here — it's read from the
    project-root `.env` (`allow_foreign_key_violation`) at CLI time.
    """
    spec_name: str
    separator: str
    column_required: bool = True
    value_required: bool = False


@dataclass(frozen=True)
class KeysColumnMapping:
    table_name: ColumnSpec
    primary_key: SplitColumnSpec
    foreign_key: SplitColumnSpec | None = None
    comments: ColumnSpec | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KeysColumnMapping":
        required = {"table_name", "primary_key"}
        missing = required - raw.keys()
        if missing:
            raise ConfigError(f"keys.column_mapping missing required entries: {sorted(missing)}")

        def _col(key: str) -> ColumnSpec:
            return _parse_column_block("keys.column_mapping", key, raw.get(key))

        def _split_col(key: str) -> SplitColumnSpec:
            block = raw.get(key)
            if not isinstance(block, dict):
                raise ConfigError(f"keys.column_mapping.{key} must be a mapping")
            spec_name = block.get("spec_name")
            if not isinstance(spec_name, str) or not spec_name:
                raise ConfigError(f"keys.column_mapping.{key}.spec_name must be a non-empty string")
            separator = block.get("separator", "|")
            if not isinstance(separator, str) or not separator:
                raise ConfigError(f"keys.column_mapping.{key}.separator must be a non-empty string")
            column_required=bool(block.get("column_required", True))
            value_required = bool(block.get("value_required", False))
            _check_required_value_required(f"keys.column_mapping.{key}", column_required, value_required)
            return SplitColumnSpec(
                spec_name=spec_name,
                column_required=column_required, value_required=value_required,
                separator=separator,
            )

        return cls(
            table_name=_col("table_name"),
            primary_key=_split_col("primary_key"),
            foreign_key=_split_col("foreign_key") if "foreign_key" in raw else None,
            comments=_col("comments") if "comments" in raw else None,
        )


@dataclass(frozen=True)
class KeysSpec:
    sheet_name: str
    column_mapping: KeysColumnMapping

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KeysSpec":
        if not isinstance(raw, dict):
            raise ConfigError("keys: must be a mapping")
        sheet_name = raw.get("sheet_name")
        if not isinstance(sheet_name, str) or not sheet_name:
            raise ConfigError("keys.sheet_name must be a non-empty string")
        cm_raw = raw.get("column_mapping")
        if not isinstance(cm_raw, dict):
            raise ConfigError("keys.column_mapping must be a mapping")
        return cls(sheet_name=sheet_name, column_mapping=KeysColumnMapping.from_dict(cm_raw))


@dataclass(frozen=True)
class CardinalityColumnSpec:
    """Cardinality column. The optional `separator` constrains what divider
    the parser accepts between the two sides of the cardinality value (e.g.
    `1 -> n` with separator `->`). When None, the parser falls back to its
    default permissive set (`:`, `->`, ` to `).

    `column_required` / `value_required` mirror ColumnSpec semantics.
    """
    spec_name: str
    separator: str | None = None
    column_required: bool = True
    value_required: bool = False


@dataclass(frozen=True)
class JoinsColumnMapping:
    source_table: ColumnSpec
    target_table: ColumnSpec
    source_column: ColumnSpec
    target_column: ColumnSpec
    join_type: ColumnSpec
    cardinality: CardinalityColumnSpec | None = None
    comment: ColumnSpec | None = None
    description: ColumnSpec | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JoinsColumnMapping":
        required = {"source_table", "target_table", "source_column", "target_column", "join_type"}
        missing = required - raw.keys()
        if missing:
            raise ConfigError(f"joins.column_mapping missing required entries: {sorted(missing)}")

        def _col(key: str) -> ColumnSpec:
            return _parse_column_block("joins.column_mapping", key, raw.get(key))

        def _cardinality_col(block: dict) -> CardinalityColumnSpec:
            spec_name = block.get("spec_name")
            if not isinstance(spec_name, str) or not spec_name:
                raise ConfigError("joins.column_mapping.cardinality.spec_name must be a non-empty string")
            separator = block.get("separator")
            if separator is not None and (not isinstance(separator, str) or not separator):
                raise ConfigError("joins.column_mapping.cardinality.separator, if set, must be a non-empty string")
            column_required=bool(block.get("column_required", True))
            value_required = bool(block.get("value_required", False))
            _check_required_value_required("joins.column_mapping.cardinality", column_required, value_required)
            return CardinalityColumnSpec(
                spec_name=spec_name,
                separator=separator,
                column_required=column_required, value_required=value_required,
            )

        cardinality_block = raw.get("cardinality")
        cardinality_spec: CardinalityColumnSpec | None = None
        if cardinality_block is not None:
            if not isinstance(cardinality_block, dict):
                raise ConfigError("joins.column_mapping.cardinality must be a mapping")
            cardinality_spec = _cardinality_col(cardinality_block)

        return cls(
            source_table=_col("source_table"),
            target_table=_col("target_table"),
            source_column=_col("source_column"),
            target_column=_col("target_column"),
            join_type=_col("join_type"),
            cardinality=cardinality_spec,
            comment=_col("comment") if "comment" in raw else None,
            description=_col("description") if "description" in raw else None,
        )


@dataclass(frozen=True)
class JoinsSpec:
    sheet_name: str
    column_mapping: JoinsColumnMapping

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JoinsSpec":
        if not isinstance(raw, dict):
            raise ConfigError("joins: must be a mapping")
        sheet_name = raw.get("sheet_name")
        if not isinstance(sheet_name, str) or not sheet_name:
            raise ConfigError("joins.sheet_name must be a non-empty string")
        cm_raw = raw.get("column_mapping")
        if not isinstance(cm_raw, dict):
            raise ConfigError("joins.column_mapping must be a mapping")
        return cls(sheet_name=sheet_name, column_mapping=JoinsColumnMapping.from_dict(cm_raw))


@dataclass(frozen=True)
class Defaults:
    column_mapping: ColumnMapping | None = None
    keys: KeysSpec | None = None
    joins: JoinsSpec | None = None

    @classmethod
    def from_yaml(cls, path: Path) -> "Defaults":
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        fields_raw = raw.get("fields")
        column_mapping = None
        if fields_raw is not None:
            if not isinstance(fields_raw, dict):
                raise ConfigError(f"{path}: 'fields' must be a mapping")
            cm_raw = fields_raw.get("column_mapping")
            if cm_raw is not None:
                if not isinstance(cm_raw, dict):
                    raise ConfigError(f"{path}: fields.column_mapping must be a mapping")
                column_mapping = ColumnMapping.from_dict(cm_raw)

        keys_raw = raw.get("keys")
        if keys_raw is None:
            raise ConfigError(
                f"{path}: missing required top-level 'keys' block. "
                f"Every defaults.yaml must declare keys.sheet_name and keys.column_mapping."
            )
        keys = KeysSpec.from_dict(keys_raw)

        joins_raw = raw.get("joins")
        joins = JoinsSpec.from_dict(joins_raw) if joins_raw is not None else None

        return cls(column_mapping=column_mapping, keys=keys, joins=joins)


@dataclass(frozen=True)
class TableSelector:
    table_name: str


@dataclass(frozen=True)
class EpicConfig:
    path: Path
    epic: str
    version: str
    spec_file_name: str
    tables: list[TableSelector] | str  # list, or ALL_TABLES sentinel
    column_mapping_override: dict[str, Any] | None  # raw, merged later

    @classmethod
    def from_yaml(cls, path: Path) -> "EpicConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top-level YAML must be a mapping")

        epic = raw.get("epic")
        if epic is None:
            raise ConfigError(f"{path}: missing required field 'epic'")
        version_raw = raw.get("version")
        if version_raw is None:
            raise ConfigError(f"{path}: missing required field 'version'")
        version = _normalize_version(version_raw)

        spec_file_name = raw.get("spec_file_name")
        if not isinstance(spec_file_name, str) or not spec_file_name:
            raise ConfigError(f"{path}: 'spec_file_name' must be a non-empty string")

        tables_raw = raw.get("tables")
        if tables_raw == "all" or tables_raw == ALL_TABLES:
            tables: list[TableSelector] | str = ALL_TABLES
        elif isinstance(tables_raw, list):
            tables_list: list[TableSelector] = []
            for i, item in enumerate(tables_raw):
                if not isinstance(item, dict) or "table_name" not in item:
                    raise ConfigError(f"{path}: tables[{i}] must be a mapping with 'table_name'")
                name = item["table_name"]
                if not isinstance(name, str) or not name:
                    raise ConfigError(f"{path}: tables[{i}].table_name must be a non-empty string")
                tables_list.append(TableSelector(table_name=name))
            tables = tables_list
        else:
            raise ConfigError(f"{path}: 'tables' must be a list or the string 'all'")

        override = None
        fields_block = raw.get("fields")
        if fields_block is not None:
            if not isinstance(fields_block, dict):
                raise ConfigError(f"{path}: 'fields' override must be a mapping")
            override = fields_block.get("column_mapping")
            if override is not None and not isinstance(override, dict):
                raise ConfigError(f"{path}: 'fields.column_mapping' override must be a mapping")

        return cls(
            path=path,
            epic=str(epic),
            version=version,
            spec_file_name=spec_file_name,
            tables=tables,
            column_mapping_override=override,
        )


@dataclass(frozen=True)
class MergedConfig:
    epic: str
    version: str
    spec_file_name: str
    tables: list[TableSelector] | str
    column_mapping: ColumnMapping
    keys: KeysSpec
    epic_config_path: Path
    joins: JoinsSpec | None = None


def _normalize_version(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        s = str(v)
        # Strip a trailing ".0" so int-typed 1.0 becomes "1.0" not "1.0"; pyyaml gives 1.0 as float
        return s
    raise ConfigError(f"version must be a string or number, got {type(v).__name__}")


def version_sort_key(v: str) -> tuple[Any, ...]:
    parts = v.split(".")
    if all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return (v,)


def discover_version_configs(epic_configs_dir: Path) -> list[Path]:
    if not epic_configs_dir.is_dir():
        raise ConfigError(f"epic configs directory not found: {epic_configs_dir}")
    out: list[Path] = []
    for p in sorted(epic_configs_dir.iterdir()):
        if p.is_file() and p.suffix in (".yaml", ".yml") and p.name != DEFAULTS_FILENAME:
            out.append(p)
    return out


def select_version_config(
    epic_configs_dir: Path,
    *,
    version: str | None = None,
    explicit_path: Path | None = None,
) -> tuple[EpicConfig, str]:
    """Return (config, reason-string-for-the-CLI-summary)."""
    if explicit_path is not None and version is not None:
        raise ConfigError("--version and --config are mutually exclusive")

    if explicit_path is not None:
        if not explicit_path.is_file():
            raise ConfigError(f"explicit config not found: {explicit_path}")
        cfg = EpicConfig.from_yaml(explicit_path)
        return cfg, f"explicit path {explicit_path}"

    candidates = discover_version_configs(epic_configs_dir)
    if not candidates:
        raise ConfigError(f"no version configs found in {epic_configs_dir}")

    loaded = [EpicConfig.from_yaml(p) for p in candidates]

    if version is not None:
        wanted = version.strip()
        matches = [c for c in loaded if c.version == wanted]
        if not matches:
            raise ConfigError(
                f"no config with version {wanted!r} in {epic_configs_dir}; "
                f"available: {[c.version for c in loaded]}"
            )
        if len(matches) > 1:
            raise ConfigError(
                f"multiple configs with version {wanted!r}: {[str(c.path) for c in matches]}"
            )
        return matches[0], f"version {wanted}"

    chosen = max(loaded, key=lambda c: version_sort_key(c.version))
    return chosen, f"highest version: {chosen.version}"


def merge(defaults: Defaults, epic: EpicConfig) -> MergedConfig:
    cm: ColumnMapping | None = defaults.column_mapping
    if epic.column_mapping_override is not None:
        if cm is None:
            cm = ColumnMapping.from_dict(epic.column_mapping_override)
        else:
            cm = ColumnMapping.from_dict(_deep_merge_column_mapping(cm, epic.column_mapping_override))

    if cm is None:
        raise ConfigError(
            f"{epic.path}: no column_mapping available (neither in defaults.yaml nor the version config)"
        )

    if defaults.keys is None:
        raise ConfigError(
            f"{epic.path}: defaults.yaml is missing the required 'keys' block"
        )

    return MergedConfig(
        epic=epic.epic,
        version=epic.version,
        spec_file_name=epic.spec_file_name,
        tables=epic.tables,
        column_mapping=cm,
        keys=defaults.keys,
        epic_config_path=epic.path,
        joins=defaults.joins,
    )


def _column_spec_to_raw(c: ColumnSpec) -> dict[str, Any]:
    return {
        "spec_name": c.spec_name,
        "column_required": c.column_required,
        "value_required": c.value_required,
    }


def _column_mapping_to_raw(cm: ColumnMapping) -> dict[str, Any]:
    """Render a ColumnMapping back into the raw-dict shape so a partial override can be merged in."""
    out: dict[str, Any] = {
        "name": _column_spec_to_raw(cm.name),
        "type": _column_spec_to_raw(cm.type),
        "description": _column_spec_to_raw(cm.description),
        "nullable": {
            "spec_name": cm.nullable.spec_name,
            "required": cm.nullable.column_required,
            "value_required": cm.nullable.value_required,
            "values": {
                "true": sorted(cm.nullable.true_values),
                "false": sorted(cm.nullable.false_values),
            },
        },
    }
    if cm.table is not None:
        out["table"] = _column_spec_to_raw(cm.table)
    for name, constraint in cm.constraints.items():
        out[name] = dict(constraint.raw_config)
    return out


def _deep_merge_column_mapping(base: ColumnMapping, override: dict[str, Any]) -> dict[str, Any]:
    base_raw = _column_mapping_to_raw(base)
    merged = _deep_merge(base_raw, override)
    return merged


def _deep_merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(out.get(k), v)
        return out
    return b
