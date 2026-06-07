from pathlib import Path

import pytest

from data_contract.type_mapping import Type, load_type_registry, parse_type, unknown_parsed_type


@pytest.fixture(scope="module")
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_double_to_number(registry):
    parsed, err = parse_type("Double", registry, sheet_row=2)
    assert err is None
    assert parsed.type is Type.DOUBLE
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
    assert parsed.type is Type.VARCHAR
    assert parsed.max_length == 384


def test_varchar_with_whitespace(registry):
    parsed, err = parse_type("VARCHAR (384)", registry, sheet_row=6)
    assert err is None
    assert parsed.type is Type.VARCHAR
    assert parsed.max_length == 384


@pytest.mark.parametrize("raw, expected", [
    ("VARCHAR(40 000 000)", 40_000_000),       # French thousand grouping with spaces
    ("VARCHAR(40 000)", 40_000),
    ("VARCHAR (40 000 000)", 40_000_000),      # whitespace before parens still tolerated
    ("varchar(1 234)", 1_234),
    ("VARCHAR(1 2 3 4)", 1234), # non-breaking spaces inside
])
def test_varchar_with_grouped_number(registry, raw, expected):
    parsed, err = parse_type(raw, registry, sheet_row=11)
    assert err is None, f"unexpected rejection: {err}"
    assert parsed.type is Type.VARCHAR
    assert parsed.max_length == expected


def test_bigint(registry):
    parsed, err = parse_type("BIGINT", registry, sheet_row=8)
    assert err is None
    assert parsed.type is Type.INTEGER


def test_text(registry):
    parsed, err = parse_type("text", registry, sheet_row=9)
    assert err is None
    assert parsed.type is Type.VARCHAR


def test_unknown_type_returns_rejection(registry):
    parsed, err = parse_type("Quaternion(8)", registry, sheet_row=10)
    assert parsed is None
    assert err is not None
    assert err.kind == "unknown_type"
    assert err.sheet_row == 10


def test_unknown_parsed_type_helper():
    parsed = unknown_parsed_type()
    assert parsed.type is Type.UNKNOWN


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
    assert registry.data_values_for(Type.VARCHAR) is None
    assert registry.data_values_for(Type.INTEGER) is None


def test_data_values_normalization_case_insensitive(tmp_path):
    yaml_path = tmp_path / "types.yaml"
    yaml_path.write_text(
        "mappings:\n"
        "  - canonical: boolean\n"
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
        "  - canonical: boolean\n"
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
        "  - canonical: boolean\n"
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
        "  - canonical: boolean\n"
        "    aliases: [bool]\n"
        "    data_values:\n"
        "      'true':  []\n"
        "      'false': ['no']\n",
        encoding="utf-8",
    )
    from data_contract.errors import ConfigError
    with pytest.raises(ConfigError, match="must be a non-empty list"):
        load_type_registry(yaml_path)
