from pathlib import Path

import pytest

from data_contract.type_mapping import Type, load_type_registry, parse_type, unknown_parsed_type


@pytest.fixture(scope="module")
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_double_to_number(registry):
    parsed, err = parse_type("Double", registry, sheet_row=2)
    assert err is None
    assert parsed.type is Type.FLOAT64
    assert parsed.max_length is None


def test_boolean_french(registry):
    parsed, err = parse_type("Booléen", registry, sheet_row=3)
    assert err is None
    assert parsed.type is Type.BOOLEAN


def test_timestamp_french(registry):
    parsed, err = parse_type("Date horodatée", registry, sheet_row=4)
    assert err is None
    assert parsed.type is Type.TIMESTAMP


def test_varchar_with_length(registry):
    parsed, err = parse_type("VARCHAR(384)", registry, sheet_row=5)
    assert err is None
    assert parsed.type is Type.STRING
    assert parsed.max_length == 384


def test_varchar_with_whitespace(registry):
    parsed, err = parse_type("VARCHAR (384)", registry, sheet_row=6)
    assert err is None
    assert parsed.type is Type.STRING
    assert parsed.max_length == 384


@pytest.mark.parametrize("raw, expected", [
    ("VARCHAR(40 000 000)", 40_000_000),       # French thousand grouping with spaces
    ("VARCHAR(40 000)", 40_000),
    ("VARCHAR (40 000 000)", 40_000_000),      # whitespace before parens still tolerated
    ("varchar(1 234)", 1_234),
    ("VARCHAR(1 2 3 4)", 1234), # non-breaking spaces inside
])
def test_varchar_with_grouped_number(registry, raw, expected):
    parsed, err = parse_type(raw, registry, sheet_row=11)
    assert err is None, f"unexpected rejection: {err}"
    assert parsed.type is Type.STRING
    assert parsed.max_length == expected


def test_bigint(registry):
    parsed, err = parse_type("BIGINT", registry, sheet_row=8)
    assert err is None
    assert parsed.type is Type.INT64


def test_text(registry):
    parsed, err = parse_type("text", registry, sheet_row=9)
    assert err is None
    assert parsed.type is Type.STRING


def test_unknown_type_returns_rejection(registry):
    parsed, err = parse_type("Quaternion(8)", registry, sheet_row=10)
    assert parsed is None
    assert err is not None
    assert err.kind == "unknown_type"
    assert err.sheet_row == 10


def test_unknown_parsed_type_helper():
    parsed = unknown_parsed_type()
    assert parsed.type is Type.UNKNOWN


def test_decimal_with_precision_and_scale(registry):
    parsed, err = parse_type("DECIMAL(38, 2)", registry, sheet_row=11)
    assert err is None
    assert parsed.type is Type.DECIMAL
    assert parsed.precision == 38
    assert parsed.scale == 2


def test_numeric_aliases_to_decimal(registry):
    parsed, err = parse_type("NUMERIC(10, 4)", registry, sheet_row=12)
    assert err is None
    assert parsed.type is Type.DECIMAL


# ---------------------------------------------------------------------------
# from_canonical_string + legacy aliasing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legacy, target", [
    ("varchar", Type.STRING),
    ("integer", Type.INT64),   # Oracle INTEGER -> NUMBER(38); widen, never narrow
    ("double",  Type.FLOAT64),
    ("float",   Type.FLOAT32),
])
def test_from_canonical_string_legacy_aliases(legacy, target):
    assert Type.from_canonical_string(legacy) is target


def test_from_canonical_string_new_canonicals():
    assert Type.from_canonical_string("string")       is Type.STRING
    assert Type.from_canonical_string("int32")        is Type.INT32
    assert Type.from_canonical_string("int64")        is Type.INT64
    assert Type.from_canonical_string("float32")      is Type.FLOAT32
    assert Type.from_canonical_string("float64")      is Type.FLOAT64
    assert Type.from_canonical_string("decimal")      is Type.DECIMAL
    assert Type.from_canonical_string("timestamp_tz") is Type.TIMESTAMP_TZ
    assert Type.from_canonical_string("binary")       is Type.BINARY


def test_from_canonical_string_normalizes_whitespace_and_case():
    assert Type.from_canonical_string("  STRING ") is Type.STRING
    assert Type.from_canonical_string("VARCHAR")   is Type.STRING


def test_from_canonical_string_unknown_raises():
    with pytest.raises(ValueError):
        Type.from_canonical_string("quaternion")


# ---------------------------------------------------------------------------
# data_values block on boolean entry
# ---------------------------------------------------------------------------


def test_boolean_data_values_loaded(registry):
    tokens = registry.data_values_for(Type.BOOLEAN)
    assert tokens is not None
    # Tokens are lower-cased + stripped at load time.
    assert "vrai" in tokens["true"]
    assert "oui" in tokens["true"]
    assert "true" in tokens["true"]
    assert "faux" in tokens["false"]
    assert "non" in tokens["false"]
    assert "false" in tokens["false"]
    # Boolean literals from YAML normalize to "true" / "false" strings.
    assert "true" in tokens["true"]
    assert "false" in tokens["false"]


def test_non_boolean_types_have_no_data_values(registry):
    assert registry.data_values_for(Type.STRING) is None
    assert registry.data_values_for(Type.INT64) is None


def test_data_values_normalization_case_insensitive(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  boolean:\n"
        "    aliases: [bool]\n"
        "    data_values:\n"
        "      'true':  ['  VRAI ', 'YES']\n"
        "      'false': ['Faux', 'NO']\n",
        encoding="utf-8",
    )
    reg = load_type_registry(yaml_path)
    tokens = reg.data_values_for(Type.BOOLEAN)
    assert tokens == {
        "true": frozenset({"vrai", "yes"}),
        "false": frozenset({"faux", "no"}),
    }


def test_data_values_duplicate_token_across_literals_rejected(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  boolean:\n"
        "    aliases: [bool]\n"
        "    data_values:\n"
        "      'true':  ['oui', 'yes']\n"
        "      'false': ['non', 'oui']\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="appears under both"):
        load_type_registry(yaml_path)


def test_data_values_must_be_mapping(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  boolean:\n"
        "    aliases: [bool]\n"
        "    data_values: ['true', 'false']\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="data_values must be a mapping"):
        load_type_registry(yaml_path)


def test_data_values_empty_list_rejected(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  boolean:\n"
        "    aliases: [bool]\n"
        "    data_values:\n"
        "      'true':  []\n"
        "      'false': ['no']\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="must be a non-empty list"):
        load_type_registry(yaml_path)


# ---------------------------------------------------------------------------
# parse_formats block on date / timestamp entries
# ---------------------------------------------------------------------------


def test_parse_formats_loaded_for_date(registry):
    formats = registry.parse_formats_for(Type.DATE)
    assert formats == ("%Y-%m-%d", "%d/%m/%Y")


def test_parse_formats_loaded_for_timestamp(registry):
    formats = registry.parse_formats_for(Type.TIMESTAMP)
    # The space-separator form is FIRST so it matches `str(datetime.datetime(...))`
    # output produced by calamine cells.
    assert formats[0] == "%Y-%m-%d %H:%M:%S"
    assert "%Y-%m-%dT%H:%M:%S" in formats


def test_parse_formats_loaded_for_timestamp_tz(registry):
    formats = registry.parse_formats_for(Type.TIMESTAMP_TZ)
    assert "%Y-%m-%dT%H:%M:%S%z" in formats


def test_parse_formats_empty_for_non_temporal_types(registry):
    assert registry.parse_formats_for(Type.STRING) == ()
    assert registry.parse_formats_for(Type.INT64) == ()
    assert registry.parse_formats_for(Type.BOOLEAN) == ()


def test_parse_formats_rejected_on_non_temporal_canonical(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  int64:\n"
        "    aliases: [int]\n"
        "    parse_formats: ['%Y']\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="only valid on DATE / TIMESTAMP"):
        load_type_registry(yaml_path)


def test_parse_formats_must_be_non_empty_list(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  date:\n"
        "    aliases: [date]\n"
        "    parse_formats: []\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="must be a non-empty list"):
        load_type_registry(yaml_path)


def test_parse_formats_preserves_case_and_order(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  date:\n"
        "    aliases: [date]\n"
        "    parse_formats: ['%Y-%m-%d', '%d/%m/%Y', '%Y%m%d']\n",
        encoding="utf-8",
    )
    reg = load_type_registry(yaml_path)
    # Order preserved (NOT lexicographic) and case preserved (%Y not %y).
    assert reg.parse_formats_for(Type.DATE) == ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d")


# ---------------------------------------------------------------------------
# new YAML shape gating
# ---------------------------------------------------------------------------


def test_load_type_registry_rejects_legacy_canonical_key(tmp_path):
    """`varchar` is not a canonical -- it's an alias of STRING. Putting it as a
    top-level key in `mappings` should fail."""
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  varchar:\n"
        "    aliases: [varchar]\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="not a recognized canonical"):
        load_type_registry(yaml_path)


def test_load_type_registry_rejects_legacy_list_shape(tmp_path):
    """The old `mappings: [- canonical: ..., aliases: ...]` shape is no longer
    supported and earns a migration error."""
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  - canonical: string\n"
        "    aliases: [string]\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="no longer supported"):
        load_type_registry(yaml_path)
