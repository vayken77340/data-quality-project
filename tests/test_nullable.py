import pytest

from data_quality.nullable import NullableMapping, parse_nullable


@pytest.fixture
def mapping_mandatory():
    return NullableMapping.from_dict({
        "spec_name": "Obligatoire",
        "value_required": True,
        "values": {
            "true": ["non", "no", "false", "0", "n"],
            "false": ["oui", "yes", "true", "1", "o", "y"],
        },
    })


@pytest.fixture
def mapping_optional():
    return NullableMapping.from_dict({
        "spec_name": "Obligatoire",
        "value_required": False,
        "values": {
            "true": ["non"],
            "false": ["oui"],
        },
    })


def test_oui_means_nullable_false(mapping_mandatory):
    val, err = parse_nullable("OUI", mapping_mandatory, sheet_row=2)
    assert err is None
    assert val is False


def test_non_means_nullable_true(mapping_mandatory):
    val, err = parse_nullable("non", mapping_mandatory, sheet_row=3)
    assert err is None
    assert val is True


def test_unknown_raw_value_is_invalid_nullable(mapping_mandatory):
    val, err = parse_nullable("maybe", mapping_mandatory, sheet_row=4)
    assert val is None
    assert err is not None
    assert err.kind == "invalid_nullable"


def test_blank_with_mandatory_true_is_missing_mandatory(mapping_mandatory):
    val, err = parse_nullable("", mapping_mandatory, sheet_row=5)
    assert val is None
    assert err is not None
    assert err.kind == "missing_mandatory"


def test_blank_with_mandatory_false_yields_none(mapping_optional):
    val, err = parse_nullable(None, mapping_optional, sheet_row=6)
    assert val is None
    assert err is None


def test_overlap_raises():
    with pytest.raises(Exception):
        NullableMapping.from_dict({
            "spec_name": "X",
            "value_required": True,
            "values": {"true": ["a"], "false": ["a"]},
        })
