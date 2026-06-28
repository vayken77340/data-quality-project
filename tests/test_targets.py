from __future__ import annotations

from pathlib import Path

import pytest

from dq_core.errors import ConfigError
from dq_core.targets import (
    TargetConfig,
    TargetOverrides,
    load_target_config,
    resolve_target_path,
)
from dq_core.type_mapping import Type, load_type_registry


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def base_registry(repo_root: Path):
    return load_type_registry(repo_root / "configs" / "types.yaml")


# ---------------------------------------------------------------------------
# load_target_config: built-in target files
# ---------------------------------------------------------------------------


def test_load_oracle_target(repo_root: Path):
    cfg = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    assert cfg.name == "oracle"
    # Oracle: bytes length_unit, decimal max_precision 38. Boolean tokens are
    # no longer target-controlled; they live on each contract field.
    assert cfg.overrides[Type.STRING].length_unit == "bytes"
    assert cfg.overrides[Type.DECIMAL].max_precision == 38
    # Target still declares physical_type for BOOLEAN (CHAR(1)) but no tokens.
    assert cfg.overrides[Type.BOOLEAN].physical_type == "CHAR(1)"


def test_load_postgres_target(repo_root: Path):
    cfg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    assert cfg.name == "postgres"
    assert cfg.overrides[Type.INT32].bounds == (-2147483648, 2147483647)
    assert cfg.overrides[Type.INT64].bounds == (
        -9223372036854775808, 9223372036854775807,
    )
    # Boolean tokens are no longer target-controlled. Target carries only the
    # physical type.
    assert cfg.overrides[Type.BOOLEAN].physical_type == "BOOLEAN"


def test_load_iceberg_target(repo_root: Path):
    cfg = load_target_config(repo_root / "configs" / "targets" / "iceberg.yaml")
    assert cfg.name == "iceberg"
    assert cfg.overrides[Type.DECIMAL].max_precision == 38
    assert cfg.overrides[Type.STRING].length_unit == "characters"


# ---------------------------------------------------------------------------
# Allowlist enforcement: misplaced keys, unknown keys
# ---------------------------------------------------------------------------


def test_unknown_override_key_rejected(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: bad\n"
        "overrides:\n"
        "  int64:\n"
        "    bogus: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown keys"):
        load_target_config(p)


def test_bounds_on_string_rejected(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: bad\n"
        "overrides:\n"
        "  string:\n"
        "    physical_type: VARCHAR\n"
        "    bounds: { min: 0, max: 100 }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="bounds is not allowed"):
        load_target_config(p)


def test_data_values_on_target_rejected(tmp_path: Path):
    """Declaring `data_values:` on ANY canonical in a target YAML is a config
    error -- boolean tokens live on the contract field, not on the target."""
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: bad\n"
        "overrides:\n"
        "  boolean:\n"
        "    physical_type: BOOLEAN\n"
        "    data_values:\n"
        "      'true':  [yes]\n"
        "      'false': [no]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown keys.*data_values"):
        load_target_config(p)


def test_length_unit_invalid_rejected(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: bad\n"
        "overrides:\n"
        "  string:\n"
        "    physical_type: VARCHAR\n"
        "    length_unit: bits\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="length_unit must be one of"):
        load_target_config(p)


def test_unknown_canonical_in_overrides_rejected(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: bad\n"
        "overrides:\n"
        "  quaternion:\n"
        "    physical_type: WAT\n"
        "    bounds: { min: 0, max: 1 }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not a recognized canonical"):
        load_target_config(p)


def test_physical_type_required(tmp_path: Path):
    """Every overrides entry must declare physical_type explicitly."""
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: missing physical_type\n"
        "overrides:\n"
        "  int32:\n"
        "    bounds: { min: 0, max: 100 }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="missing required key 'physical_type'"):
        load_target_config(p)


def test_physical_type_must_be_non_empty_string(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "name: x\n"
        "description: x\n"
        "overrides:\n"
        "  int32:\n"
        "    physical_type: \"\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="physical_type must be a non-empty string"):
        load_target_config(p)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_resolve_target_path_repo_root(repo_root: Path):
    p = resolve_target_path("postgres", repo_root=repo_root, epic_dir=None)
    assert p == repo_root / "configs" / "targets" / "postgres.yaml"


def test_resolve_target_path_per_epic_wins(tmp_path: Path, repo_root: Path):
    # Stub a per-epic override.
    epic_dir = tmp_path / "epics" / "X"
    epic_targets = epic_dir / "configs" / "targets"
    epic_targets.mkdir(parents=True)
    custom = epic_targets / "postgres.yaml"
    custom.write_text("name: postgres\ndescription: epic-local\n", encoding="utf-8")
    p = resolve_target_path("postgres", repo_root=repo_root, epic_dir=epic_dir)
    assert p == custom


def test_resolve_target_path_unknown_raises(repo_root: Path):
    with pytest.raises(ConfigError, match="not found"):
        resolve_target_path("nope", repo_root=repo_root, epic_dir=None)


# ---------------------------------------------------------------------------
# TypeRegistry.with_target overlay behaviour
# ---------------------------------------------------------------------------


def test_with_target_none_is_self(base_registry):
    assert base_registry.with_target(None) is base_registry


def test_with_target_overlays_bounds(base_registry, repo_root: Path):
    pg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    merged = base_registry.with_target(pg)
    assert base_registry.bounds_for(Type.INT32) is None
    assert merged.bounds_for(Type.INT32) == (-2147483648, 2147483647)


def test_with_target_does_not_override_boolean_tokens(base_registry, repo_root: Path):
    """Boolean tokens live on the contract field, not on the target. Applying
    a target overlay must leave the registry's universal token list untouched."""
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    base_true = base_registry.data_values_for(Type.BOOLEAN)["true"]
    assert "vrai" in base_true
    # With Oracle applied the registry still returns the universal list.
    merged_true = merged.data_values_for(Type.BOOLEAN)["true"]
    assert merged_true == base_true


def test_length_unit_for_string_requires_active_target(base_registry, repo_root: Path):
    from dq_core.errors import ConfigError
    # Base registry has no STRING overlay -> length_unit lookup raises clean error.
    with pytest.raises(ConfigError, match="length_unit is not defined"):
        base_registry.length_unit_for(Type.STRING)


def test_with_target_overlays_string_length_unit(base_registry, repo_root: Path):
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    assert merged.length_unit_for(Type.STRING) == "bytes"
    pg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    merged_pg = base_registry.with_target(pg)
    assert merged_pg.length_unit_for(Type.STRING) == "characters"


def test_with_target_overlays_parse_formats(base_registry, repo_root: Path):
    base_formats = base_registry.parse_formats_for(Type.TIMESTAMP)
    assert base_formats[0] == "%Y-%m-%d %H:%M:%S"
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    merged_formats = merged.parse_formats_for(Type.TIMESTAMP)
    # Oracle declares its own list (includes %d-%b-%Y); base's `%Y-%m-%dT%H:%M:%S` is NOT inherited.
    assert "%d-%b-%Y %H:%M:%S" in merged_formats
    assert "%Y-%m-%dT%H:%M:%S" not in merged_formats


def test_with_target_does_not_mutate_base(base_registry, repo_root: Path):
    pg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    _ = base_registry.with_target(pg)
    # Base registry's accessors are unchanged.
    assert base_registry.bounds_for(Type.INT32) is None
    assert "vrai" in base_registry.data_values_for(Type.BOOLEAN)["true"]


def test_max_precision_for_decimal(base_registry, repo_root: Path):
    assert base_registry.max_precision_for(Type.DECIMAL) is None
    iceberg = load_target_config(repo_root / "configs" / "targets" / "iceberg.yaml")
    merged = base_registry.with_target(iceberg)
    assert merged.max_precision_for(Type.DECIMAL) == 38


# ---------------------------------------------------------------------------
# physical_type_for: per-target physical-name lookup + placeholder substitution
# ---------------------------------------------------------------------------


def _stub_field(name: str, t: Type, **attrs):
    """Minimal stand-in for FieldContract carrying the attrs physical_type_for needs."""
    class _F:
        pass
    f = _F()
    f.name = name
    f.type = t
    f.max_length = attrs.get("max_length")
    f.precision = attrs.get("precision")
    f.scale = attrs.get("scale")
    return f


def test_physical_type_for_base_returns_none(base_registry):
    f = _stub_field("x", Type.INT64)
    assert base_registry.physical_type_for(f) is None


def test_physical_type_for_oracle_int64(base_registry, repo_root: Path):
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    assert merged.physical_type_for(_stub_field("id", Type.INT64)) == "NUMBER(19, 0)"


def test_physical_type_for_oracle_string_substitutes_max_length(
    base_registry, repo_root: Path,
):
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    rendered = merged.physical_type_for(_stub_field("s", Type.STRING, max_length=100))
    assert rendered == "VARCHAR2(100 BYTE)"


def test_physical_type_for_oracle_decimal_substitutes_precision_scale(
    base_registry, repo_root: Path,
):
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    rendered = merged.physical_type_for(
        _stub_field("d", Type.DECIMAL, precision=10, scale=2)
    )
    assert rendered == "NUMBER(10, 2)"


def test_physical_type_for_postgres_int_types(base_registry, repo_root: Path):
    pg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    merged = base_registry.with_target(pg)
    assert merged.physical_type_for(_stub_field("a", Type.INT32)) == "INTEGER"
    assert merged.physical_type_for(_stub_field("b", Type.INT64)) == "BIGINT"


def test_physical_type_for_postgres_string(base_registry, repo_root: Path):
    pg = load_target_config(repo_root / "configs" / "targets" / "postgres.yaml")
    merged = base_registry.with_target(pg)
    rendered = merged.physical_type_for(_stub_field("s", Type.STRING, max_length=50))
    assert rendered == "VARCHAR(50)"


def test_physical_type_for_iceberg_decimal(base_registry, repo_root: Path):
    iceberg = load_target_config(repo_root / "configs" / "targets" / "iceberg.yaml")
    merged = base_registry.with_target(iceberg)
    rendered = merged.physical_type_for(
        _stub_field("d", Type.DECIMAL, precision=38, scale=6)
    )
    assert rendered == "decimal(38, 6)"


def test_physical_type_for_missing_placeholder_raises(
    base_registry, repo_root: Path,
):
    """STRING field with no max_length under Oracle template raises a clear error."""
    oracle = load_target_config(repo_root / "configs" / "targets" / "oracle.yaml")
    merged = base_registry.with_target(oracle)
    with pytest.raises(ConfigError, match="does not declare max_length"):
        merged.physical_type_for(_stub_field("s", Type.STRING))
