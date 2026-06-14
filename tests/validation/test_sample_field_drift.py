"""Tests for the source-schema drift checks.

`field_names_from_sample` and `field_types_from_sample` compare the parser-
discovered schema (`ParserSchema` exposed by `FileParser.parse_file`)
against the contract. Both default off; both emit `warning` severity;
both skip silently when no source file reported the relevant schema
component.
"""

from __future__ import annotations

import pytest

from data_contract.contract import Contract, FieldContract
from data_contract.data_parsers.base import FileParser, ParsedFile, ParserSchema
from data_contract.data_parsers.json_parser import JsonParser
from data_contract.type_mapping import Type
from data_contract.validation.checks.sample_field_drift import (
    check_field_names_from_sample,
    check_field_types_from_sample,
)


def _contract(*fields: FieldContract) -> Contract:
    return Contract(
        version="1.0", epic="T", generated_at="",
        spec_file="", spec_sheet="T", table="T",
        fields=list(fields),
    )


def _f(name: str, t: Type = Type.STRING) -> FieldContract:
    return FieldContract(name=name, type=t, nullable=True, description=None)


# ---------------------------------------------------------------------------
# check_field_names_from_sample
# ---------------------------------------------------------------------------


def test_names_match_clean_yields_no_warnings():
    contract = _contract(_f("a"), _f("b"))
    schemas = [("file.csv", ParserSchema(column_names=["a", "b"]))]
    assert check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    ) == []


def test_names_source_has_extra_column_warns():
    contract = _contract(_f("a"), _f("b"))
    schemas = [("file.csv", ParserSchema(column_names=["a", "b", "extra"]))]
    out = check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    )
    assert len(out) == 1
    assert out[0].field == "extra"
    assert out[0].severity == "warning"
    assert out[0].offending_value["direction"] == "source_only"
    assert out[0].offending_value["source_files"] == ["file.csv"]


def test_names_contract_has_extra_field_warns():
    contract = _contract(_f("a"), _f("b"), _f("c"))
    schemas = [("file.csv", ParserSchema(column_names=["a", "b"]))]
    out = check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    )
    assert len(out) == 1
    assert out[0].field == "c"
    assert out[0].offending_value["direction"] == "contract_only"


def test_names_both_directions_independent():
    contract = _contract(_f("a"), _f("b"), _f("missing_in_source"))
    schemas = [(
        "file.csv",
        ParserSchema(column_names=["a", "b", "extra_in_source"]),
    )]
    out = check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    )
    assert len(out) == 2
    fields = {v.field: v.offending_value["direction"] for v in out}
    assert fields == {
        "extra_in_source":    "source_only",
        "missing_in_source":  "contract_only",
    }


def test_names_silent_when_no_file_reports_names():
    """All schemas None or all .column_names None -> no warnings, even
    when contract has fields. The parser format doesn't carry names;
    there's nothing to compare."""
    contract = _contract(_f("a"), _f("b"))
    schemas = [("file.bin", None), ("other.bin", None)]
    assert check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    ) == []


def test_names_union_across_multiple_files():
    """A column appearing in either file is enough -- no warning for it.
    A column missing from BOTH source schemas and the contract isn't
    flagged either (it doesn't exist)."""
    contract = _contract(_f("a"), _f("b"))
    schemas = [
        ("a.csv", ParserSchema(column_names=["a"])),
        ("b.csv", ParserSchema(column_names=["b"])),
    ]
    assert check_field_names_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
    ) == []


# ---------------------------------------------------------------------------
# check_field_types_from_sample
# ---------------------------------------------------------------------------


def _json_parser():
    """A real JsonParser instance, used so the test exercises the actual
    SOURCE_TYPE_ALIASES dict and normalize_source_type method."""
    return JsonParser({
        "encoding": "utf-8",
        "header_path": "data.report_header",
        "rows_path": "data.report_row",
        "name_key": "name",
        "null_tokens": [""],
    })


def test_types_match_clean_yields_no_warnings():
    contract = _contract(_f("a", Type.STRING))
    schemas = [(
        "f.json",
        ParserSchema(column_names=["a"], column_types={"a": "java.lang.String"}),
    )]
    assert check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    ) == []


def test_types_mismatch_warns_with_normalisation_detail():
    """Contract says STRING, source says java.lang.Integer (normalises to
    int32) -> one warning with both sides in `offending_value`."""
    contract = _contract(_f("a", Type.STRING))
    schemas = [(
        "f.json",
        ParserSchema(column_names=["a"], column_types={"a": "java.lang.Integer"}),
    )]
    out = check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    )
    assert len(out) == 1
    assert out[0].field == "a"
    assert out[0].kind == "field_type_drift"
    assert out[0].severity == "warning"
    assert out[0].offending_value["source_type"] == "java.lang.Integer"
    assert out[0].offending_value["normalised_to"] == "int32"
    assert out[0].offending_value["contract_type"] == "string"


def test_types_unknown_source_type_emits_info():
    """When the parser can't normalise a source-side type (no entry in
    SOURCE_TYPE_ALIASES), emit a `field_type_unknown` info violation so
    the operator can extend the mapping."""
    contract = _contract(_f("a", Type.STRING))
    schemas = [(
        "f.json",
        ParserSchema(column_names=["a"], column_types={"a": "com.acme.CustomType"}),
    )]
    out = check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    )
    assert len(out) == 1
    assert out[0].kind == "field_type_unknown"
    assert out[0].severity == "info"
    assert out[0].offending_value["source_type"] == "com.acme.CustomType"


def test_types_silent_when_no_file_reports_types():
    """All schemas have None column_types -> no warnings. CSV runs land
    here -- CSV doesn't carry types."""
    contract = _contract(_f("a", Type.STRING))
    schemas = [(
        "f.csv",
        ParserSchema(column_names=["a"], column_types=None),
    )]
    assert check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    ) == []


def test_types_ignores_columns_not_in_contract():
    """A column declared by the source but not by the contract is the
    `field_names_from_sample` check's job -- types check skips it silently."""
    contract = _contract(_f("a", Type.STRING))
    schemas = [(
        "f.json",
        ParserSchema(
            column_names=["a", "ghost"],
            column_types={"a": "java.lang.String", "ghost": "java.lang.Integer"},
        ),
    )]
    out = check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    )
    assert out == []


def test_types_dedups_same_disagreement_across_multiple_files():
    """Two files declare the same `a: java.lang.Integer` against a
    `string` contract -> one warning, not two."""
    contract = _contract(_f("a", Type.STRING))
    same = ParserSchema(column_names=["a"], column_types={"a": "java.lang.Integer"})
    schemas = [("f1.json", same), ("f2.json", same)]
    out = check_field_types_from_sample(
        table="T", per_file_schemas=schemas, contract=contract,
        parser=_json_parser(),
    )
    assert len(out) == 1
