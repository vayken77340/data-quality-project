"""Per-epic generation config: column mappings, version configs, merge rules.

Owns the dataclasses that model the two YAML inputs the build pipeline reads:
`specs_parsing.yaml` (column-mapping + keys/joins sheet shapes, shared across
versions; `Defaults`) and `configs/contracts/<version>.yaml` (per-version
overrides, including `target` and `tables`; `EpicConfig`). `merge()` combines
the two into the `MergedConfig` consumed by `generation/pipeline.py`.

Also owns the spec-column reference parsers (`ColumnMapping`, `KeysSpec`,
`JoinsSpec`) plus the version-discovery / version-ordering utilities the CLI
uses to walk every config under `configs/contracts/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from data_contract.core.column_ref import (
    UNSET as _UNSET,
    CardinalityColumnRef,
    ColumnRef,
    SeparatedColumnRef,
    parse_cardinality_column_ref,
    parse_column_ref,
    parse_separated_column_ref,
)
from data_contract.core.yaml_io import load_yaml, load_yaml_mapping
from data_contract.errors import ConfigError
from data_contract.field_constraints import REGISTRY as CONSTRAINT_REGISTRY
from data_contract.field_constraints.base import FieldConstraint
from data_contract.generation.nullable import NullableMapping


# ---------------------------------------------------------------------------
# Shared `from_dict` plumbing
# ---------------------------------------------------------------------------
#
# The three `*ColumnMapping.from_dict` methods below all check that a set of
# required entries is present, then parse each entry via `parse_column_ref`
# with the same `prefix` repeated per call. These two helpers collapse that
# pattern; class-specific concerns (which entries are required, optional, or
# need a non-plain ColumnRef parser) stay inline.


def _check_required(raw: dict[str, Any], required: set[str], *, ctx: str) -> None:
    missing = required - raw.keys()
    if missing:
        raise ConfigError(
            f"{ctx} missing required entries: {sorted(missing)}. "
            f"Declare them in configs/specs_parsing.yaml under the matching "
            f"section; see configs/specs_parsing.yaml for the full shape."
        )


def _make_col_parser(raw: dict[str, Any], *, prefix: str) -> Callable[[str], ColumnRef]:
    def col(key: str) -> ColumnRef:
        return parse_column_ref(raw.get(key) or {}, prefix=prefix, key=key)
    return col


ALL_TABLES = "__ALL__"
SPECS_PARSING_FILENAME = "specs_parsing.yaml"
VALIDATION_FILENAME = "validation.yaml"
# Per-version contract configs live under `epics/<E>/configs/<CONTRACT_CONFIGS_SUBDIR>/`.
# Keeping them in a dedicated folder means discovery is "list every YAML here" --
# no filename-based exclusion list, no risk that a new top-level config file
# accidentally gets parsed as a version.
CONTRACT_CONFIGS_SUBDIR = "contracts"


_CORE_KEYS = frozenset({"name", "db_name", "type", "description", "nullable", "table"})


@dataclass(frozen=True)
class ColumnMapping:
    name: ColumnRef
    type: ColumnRef
    description: ColumnRef
    nullable: NullableMapping
    # Optional spec column whose value, when present, becomes the contract
    # field's `name` verbatim (bypassing slugify). Maps to the "Nom BDD"
    # column in the standard specs_parsing.yaml.
    db_name: ColumnRef | None = None
    table: ColumnRef | None = None
    constraints: dict[str, FieldConstraint] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ColumnMapping":
        _check_required(
            raw, {"name", "type", "description", "nullable"}, ctx="column_mapping",
        )
        col = _make_col_parser(raw, prefix="column_mapping")

        nullable_block = raw.get("nullable")
        if not isinstance(nullable_block, dict):
            raise ConfigError("column_mapping.nullable must be a mapping")
        nullable = NullableMapping.from_dict(nullable_block)

        table_block = raw.get("table")
        if table_block is None:
            table = None
        else:
            table = parse_column_ref(table_block, prefix="column_mapping", key="table")

        db_name_block = raw.get("db_name")
        if db_name_block is None:
            db_name = None
        else:
            db_name = parse_column_ref(db_name_block, prefix="column_mapping", key="db_name")

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
            db_name=db_name,
            table=table,
            constraints=constraints,
        )


@dataclass(frozen=True)
class KeysColumnMapping:
    table_name: ColumnRef
    primary_key: SeparatedColumnRef
    foreign_key: SeparatedColumnRef | None = None
    comments: ColumnRef | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KeysColumnMapping":
        _check_required(raw, {"table_name", "primary_key"}, ctx="keys.column_mapping")
        _col = _make_col_parser(raw, prefix="keys.column_mapping")

        def _split_col(key: str) -> SeparatedColumnRef:
            return parse_separated_column_ref(
                raw.get(key) or {}, prefix="keys.column_mapping", key=key,
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
class JoinsColumnMapping:
    source_table: ColumnRef
    target_table: ColumnRef
    source_column: ColumnRef
    target_column: ColumnRef
    join_type: ColumnRef
    cardinality: CardinalityColumnRef | None = None
    comment: ColumnRef | None = None
    description: ColumnRef | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JoinsColumnMapping":
        _check_required(
            raw,
            {"source_table", "target_table", "source_column", "target_column", "join_type"},
            ctx="joins.column_mapping",
        )
        _col = _make_col_parser(raw, prefix="joins.column_mapping")

        cardinality_block = raw.get("cardinality")
        cardinality_spec: CardinalityColumnRef | None = None
        if cardinality_block is not None:
            if not isinstance(cardinality_block, dict):
                raise ConfigError("joins.column_mapping.cardinality must be a mapping")
            cardinality_spec = parse_cardinality_column_ref(
                cardinality_block, prefix="joins.column_mapping", key="cardinality",
            )

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
        raw = load_yaml(path)
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
                f"Every specs_parsing.yaml must declare keys.sheet_name and keys.column_mapping."
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
    target: str
    tables: list[TableSelector] | str  # list, or ALL_TABLES sentinel
    column_mapping_override: dict[str, Any] | None  # raw, merged later

    @classmethod
    def from_yaml(cls, path: Path) -> "EpicConfig":
        raw = load_yaml_mapping(path, what="epic config")

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

        target_raw = raw.get("target")
        if target_raw is None:
            raise ConfigError(
                f"{path}: missing required field 'target'. Declare the target "
                f"database the generated contracts are for -- e.g. `target: oracle`."
            )
        if not isinstance(target_raw, str) or not target_raw:
            raise ConfigError(f"{path}: 'target' must be a non-empty string")

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
            target=target_raw,
            tables=tables,
            column_mapping_override=override,
        )


@dataclass(frozen=True)
class MergedConfig:
    epic: str
    version: str
    spec_file_name: str
    target: str
    tables: list[TableSelector] | str
    column_mapping: ColumnMapping
    keys: KeysSpec
    epic_config_path: Path
    joins: JoinsSpec | None = None


def _normalize_version(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    raise ConfigError(f"version must be a string or number, got {type(v).__name__}")


def version_sort_key(v: str) -> tuple[Any, ...]:
    parts = v.split(".")
    if all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return (v,)


def discover_version_configs(epic_configs_dir: Path) -> list[Path]:
    """Return every per-version contract YAML under `<epic_configs_dir>/contracts/`.

    The subdir layout (vs. the old flat layout that mixed version configs
    with `validation.yaml` and `specs_parsing.yaml`) lets discovery be a
    simple glob -- no filename-based exclusion list to maintain.
    """
    if not epic_configs_dir.is_dir():
        raise ConfigError(f"epic configs directory not found: {epic_configs_dir}")
    versions_dir = epic_configs_dir / CONTRACT_CONFIGS_SUBDIR
    if not versions_dir.is_dir():
        raise ConfigError(
            f"no contract version configs found at {versions_dir}; "
            f"create the folder and add at least one version YAML "
            f"(e.g. {versions_dir / 'v1.0.yaml'})."
        )
    out: list[Path] = []
    for p in sorted(versions_dir.iterdir()):
        if p.is_file() and p.suffix in (".yaml", ".yml"):
            out.append(p)
    return out


def merge(defaults: Defaults, epic: EpicConfig) -> MergedConfig:
    cm: ColumnMapping | None = defaults.column_mapping
    if epic.column_mapping_override is not None:
        if cm is None:
            cm = ColumnMapping.from_dict(epic.column_mapping_override)
        else:
            cm = ColumnMapping.from_dict(
                _deep_merge(_column_mapping_to_raw(cm), epic.column_mapping_override)
            )

    if cm is None:
        raise ConfigError(
            f"{epic.path}: no column_mapping available (neither in specs_parsing.yaml nor the version config)"
        )

    if defaults.keys is None:
        raise ConfigError(
            f"{epic.path}: specs_parsing.yaml is missing the required 'keys' block"
        )

    return MergedConfig(
        epic=epic.epic,
        version=epic.version,
        spec_file_name=epic.spec_file_name,
        target=epic.target,
        tables=epic.tables,
        column_mapping=cm,
        keys=defaults.keys,
        epic_config_path=epic.path,
        joins=defaults.joins,
    )


# ---------------------------------------------------------------------------
# Deep-merge plumbing for the version-config column_mapping override.
#
# An EpicConfig override is partial raw YAML; the base ColumnMapping is a
# parsed dataclass. We re-render the base to its raw shape, deep-merge the
# override on top, then re-parse. Two allocations is the cost; correctness
# is one round-trip through the same `from_dict` the YAML parser uses.
# ---------------------------------------------------------------------------


def _column_ref_to_raw(c: ColumnRef) -> dict[str, Any]:
    out: dict[str, Any] = {
        "spec_name": c.spec_name,
        "column_required": c.column_required,
    }
    if c.has_default:
        out["default_value"] = c.default_value
    return out


def _column_mapping_to_raw(cm: ColumnMapping) -> dict[str, Any]:
    nullable_raw: dict[str, Any] = {
        "spec_name": cm.nullable.spec_name,
        "required": cm.nullable.column_required,
        "values": {
            "true": sorted(cm.nullable.true_values),
            "false": sorted(cm.nullable.false_values),
        },
    }
    if cm.nullable.has_default:
        nullable_raw["default_value"] = cm.nullable.default_value
    out: dict[str, Any] = {
        "name": _column_ref_to_raw(cm.name),
        "type": _column_ref_to_raw(cm.type),
        "description": _column_ref_to_raw(cm.description),
        "nullable": nullable_raw,
    }
    if cm.db_name is not None:
        out["db_name"] = _column_ref_to_raw(cm.db_name)
    if cm.table is not None:
        out["table"] = _column_ref_to_raw(cm.table)
    for name, constraint in cm.constraints.items():
        out[name] = dict(constraint.raw_config)
    return out


def _deep_merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(out.get(k), v)
        return out
    return b
