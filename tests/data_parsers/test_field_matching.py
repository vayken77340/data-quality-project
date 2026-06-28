"""Tests for the generic `field_matching_policy` system on FileParser.

The policy implementation lives in `data_parsers.field_matching`; FileParser
calls `apply_policy(...)` once per file inside its `read()` loop. These tests
use a tiny in-memory parser whose `parse_file` emits a known LazyFrame; the
focus is the policy itself, not any particular file format.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from data_contract.data_parsers.base import (
    FileParser,
    ParsedFile,
    ParserSchema,
)
from data_contract.data_parsers.field_matching import (
    _normalize_for_matching,
    _similarity_score,
)
from dq_core.errors import ConfigError


# ---------------------------------------------------------------------------
# Fixture parser: emits whatever columns + rows we feed it
# ---------------------------------------------------------------------------


class _FixtureParser(FileParser):
    """In-memory parser. `parse_file` yields a Polars LazyFrame with
    whatever column names / rows the test supplied. Keeps the test focused
    on the matching policy, not file-format quirks."""

    name = "fixture"
    extensions = (".tst",)
    PARSER_PARAMS = ()

    def __init__(self, rows: list[dict], column_names: list[str], *, policy: str | None = None) -> None:
        params = {} if policy is None else {"field_matching_policy": policy}
        super().__init__(params=params)
        self._rows = rows
        self._column_names = column_names

    def parse_file(self, path: Path, *, table_name_hint=None) -> ParsedFile:
        return ParsedFile(
            rows=pl.DataFrame(self._rows, schema={c: pl.String for c in self._column_names}).lazy(),
            schema=ParserSchema(column_names=list(self._column_names), column_types=None),
        )


def _touch(tmp_path: Path, name: str = "a.tst") -> Path:
    p = tmp_path / name
    p.write_text("", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Normalization + scoring (internals)
# ---------------------------------------------------------------------------


def test_normalize_collapses_case_whitespace_punctuation():
    assert _normalize_for_matching("User_ID") == "user id"
    assert _normalize_for_matching("USER-ID") == "user id"
    assert _normalize_for_matching("  user.id  ") == "user id"
    assert _normalize_for_matching("UserID") == "userid"


def test_similarity_score_handles_case_and_punctuation():
    # Identical after normalization -> max score.
    assert _similarity_score("User_ID", "user id") == 1.0
    # Typos: SequenceMatcher catches them.
    assert _similarity_score("user_id", "uesr_id") >= 0.7
    # Token overlap: "customer number" vs "customer no" -- jaccard 0.33,
    # char ratio in the high 0.7s -> max around 0.78.
    assert _similarity_score("Customer Number", "customer_no") >= 0.6


def test_similarity_score_misses_semantic_equivalents():
    # The policy explicitly does NOT try to be smarter than string similarity.
    # "Record Number" vs "Report No." share only "No"/"Number" and their
    # character sequences diverge -- this is field_mapping territory.
    score = _similarity_score("Record Number", "Report No.")
    assert score < 0.8


# ---------------------------------------------------------------------------
# Policy validation at __init__ time
# ---------------------------------------------------------------------------


def test_unknown_policy_raises():
    with pytest.raises(ConfigError, match="field_matching_policy"):
        _FixtureParser([], [], policy="fuzzy_wuzzy")


def test_typo_gate_accepts_field_matching_policy_without_redeclaration():
    """`field_matching_policy` is not in PARSER_PARAMS but `_BASE_PARSER_PARAMS`
    lets the typo-gate accept it on any parser."""
    # Construct via the base FileParser path to bypass _FixtureParser's
    # custom signature; just exercise the param-validation logic.
    class _TinyParser(FileParser):
        name = "tiny"
        extensions = (".tst",)
        PARSER_PARAMS = ()

        def parse_file(self, path, *, table_name_hint=None):
            raise NotImplementedError

    _TinyParser({"field_matching_policy": "exact"})       # accepted
    with pytest.raises(ConfigError, match="unknown keys"):
        _TinyParser({"bogus_key": True})


# ---------------------------------------------------------------------------
# Positional policy
# ---------------------------------------------------------------------------


def test_positional_renames_columns_by_index(tmp_path):
    parser = _FixtureParser(
        rows=[{"A": "1", "B": "foo", "C": "10"}],
        column_names=["A", "B", "C"],
        policy="positional",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None), ("price", None)],
    ).frame.collect()
    # Columns now carry contract names; values landed under their
    # positional contract field.
    assert "id" in df.columns and df["id"].to_list() == ["1"]
    assert "name" in df.columns and df["name"].to_list() == ["foo"]
    assert "price" in df.columns and df["price"].to_list() == ["10"]


def test_positional_no_op_when_already_aligned(tmp_path):
    parser = _FixtureParser(
        rows=[{"id": "1", "name": "foo"}],
        column_names=["id", "name"],
        policy="positional",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None)],
    ).frame.collect()
    assert df["id"].to_list() == ["1"]
    assert df["name"].to_list() == ["foo"]


def test_positional_fewer_existing_than_contract(tmp_path):
    """Existing columns are renamed positionally; the absent contract field
    surfaces through column_missing downstream (not the parser's concern)."""
    parser = _FixtureParser(
        rows=[{"A": "1", "B": "foo"}],
        column_names=["A", "B"],
        policy="positional",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None), ("price", None)],
    ).frame.collect()
    assert "id" in df.columns
    assert "name" in df.columns
    assert "price" not in df.columns   # missing -- runner emits column_missing


def test_positional_extra_existing_left_as_extra(tmp_path):
    """Extras stay under their original names; extra_column catches them."""
    parser = _FixtureParser(
        rows=[{"A": "1", "B": "foo", "C": "10", "D": "junk"}],
        column_names=["A", "B", "C", "D"],
        policy="positional",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None), ("price", None)],
    ).frame.collect()
    assert {"id", "name", "price", "D"} <= set(df.columns)


def test_positional_collision_raises(tmp_path):
    """If a contract name lands on position i but position j>=N already has
    that name, Polars would silently produce duplicates -- ConfigError."""
    parser = _FixtureParser(
        rows=[{"A": "1", "B": "foo", "C": "junk", "id": "OOPS"}],
        column_names=["A", "B", "C", "id"],
        policy="positional",
    )
    with pytest.raises(ConfigError, match="duplicate columns"):
        parser.read(
            [_touch(tmp_path)], contract_fields=[("id", None), ("name", None), ("price", None)],
        ).frame.collect()


# ---------------------------------------------------------------------------
# Exact policy
# ---------------------------------------------------------------------------


def test_exact_keeps_matching_columns_as_is(tmp_path):
    parser = _FixtureParser(
        rows=[{"id": "1", "name": "foo"}],
        column_names=["id", "name"],
        policy="exact",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None)],
    ).frame.collect()
    assert df["id"].to_list() == ["1"]
    assert df["name"].to_list() == ["foo"]


def test_exact_leaves_mismatched_columns_alone(tmp_path):
    """No rename. Mismatched columns surface through column_missing /
    extra_column downstream -- the policy itself stays silent."""
    parser = _FixtureParser(
        rows=[{"ID": "1", "Name": "foo"}],
        column_names=["ID", "Name"],
        policy="exact",
    )
    df = parser.read(
        [_touch(tmp_path)], contract_fields=[("id", None), ("name", None)],
    ).frame.collect()
    # No rename happened -- still capitalised, still mismatched.
    assert "ID" in df.columns
    assert "Name" in df.columns
    assert "id" not in df.columns
    assert "name" not in df.columns


# ---------------------------------------------------------------------------
# Similarity policy
# ---------------------------------------------------------------------------


def test_similarity_matches_case_variants(tmp_path):
    parser = _FixtureParser(
        rows=[{"ID": "1", "User Name": "foo"}],
        column_names=["ID", "User Name"],
        policy="similarity",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("id", None), ("user_name", None)],
        similarity_threshold=0.8,
    ).frame.collect()
    assert df["id"].to_list() == ["1"]
    assert df["user_name"].to_list() == ["foo"]


def test_similarity_respects_threshold(tmp_path):
    """Below-threshold matches stay unmapped."""
    parser = _FixtureParser(
        rows=[{"Completely Different": "1"}],
        column_names=["Completely Different"],
        policy="similarity",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("id", None)],
        similarity_threshold=0.8,
    ).frame.collect()
    # No rename -- threshold protects against accidental matches.
    assert "Completely Different" in df.columns
    assert "id" not in df.columns


def test_similarity_greedy_doesnt_double_claim(tmp_path):
    """Two existing columns both look similar to one contract field; only
    the first to scan wins, second stays unmapped."""
    parser = _FixtureParser(
        rows=[{"user_id": "1", "user-id": "X"}],
        column_names=["user_id", "user-id"],
        policy="similarity",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("userid", None)],
        similarity_threshold=0.6,
    ).frame.collect()
    # The first column claims "userid"; the second has no contract field left.
    assert "userid" in df.columns
    # The unclaimed source column survives under its original name.
    assert "user-id" in df.columns or "user_id" in df.columns
    # Greedy means EXACTLY one rename, no duplicate target.
    assert df.columns.count("userid") == 1


# ---------------------------------------------------------------------------
# No-op fallback when used outside the runner
# ---------------------------------------------------------------------------


def test_no_contract_fields_is_no_op(tmp_path):
    """Parsers used standalone (no runner passing `contract_fields`) skip
    the matching step entirely -- the LazyFrame passes through with its
    parser-emitted column names."""
    parser = _FixtureParser(
        rows=[{"foo": "1"}],
        column_names=["foo"],
        policy="positional",
    )
    # contract_fields omitted -- default is None, so matching is a no-op.
    df = parser.read([_touch(tmp_path)]).frame.collect()
    assert "foo" in df.columns


# ---------------------------------------------------------------------------
# source_name preference: exact + similarity policies match against the raw
# spec header first, then fall back to the contract's slugified `name`.
# ---------------------------------------------------------------------------


def test_exact_prefers_source_name_over_name(tmp_path):
    """When the file's header is the business-friendly spec value, the
    `exact` policy renames it to the contract's database `name`."""
    parser = _FixtureParser(
        rows=[{"Reference Number": "1", "Email": "x@y"}],
        column_names=["Reference Number", "Email"],
        policy="exact",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("reference_number", "Reference Number"), ("email", None)],
    ).frame.collect()
    assert "reference_number" in df.columns
    assert df["reference_number"].to_list() == ["1"]
    # "Email" lacks a source_name, so it had to match `name` exactly --
    # case-sensitive, so it doesn't.
    assert "Email" in df.columns
    assert "email" not in df.columns


def test_exact_falls_back_to_name_when_source_name_absent(tmp_path):
    """When `source_name` isn't set, exact-match against `name` still works."""
    parser = _FixtureParser(
        rows=[{"id": "1"}],
        column_names=["id"],
        policy="exact",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("id", None)],
    ).frame.collect()
    assert "id" in df.columns


def test_similarity_prefers_source_name_for_candidate(tmp_path):
    """Similarity scoring uses the business-friendly header as the candidate
    when set -- so an input column close to the source name wins."""
    parser = _FixtureParser(
        rows=[{"Reference No.": "1"}],
        column_names=["Reference No."],
        policy="similarity",
    )
    df = parser.read(
        [_touch(tmp_path)],
        contract_fields=[("reference_number", "Reference Number")],
        similarity_threshold=0.6,
    ).frame.collect()
    assert "reference_number" in df.columns
    assert df["reference_number"].to_list() == ["1"]


# ---------------------------------------------------------------------------
# Multi-file: rename per file BEFORE concat
# ---------------------------------------------------------------------------


def test_positional_multi_file_with_disagreeing_headers(tmp_path):
    """Two files emit disagreeing column names, both positionally aligned
    with the contract. After per-file rename + concat, the unified frame
    has contract names for both rows -- no scrambling."""
    file_a = tmp_path / "a.tst"
    file_a.write_text("", encoding="utf-8")
    file_b = tmp_path / "b.tst"
    file_b.write_text("", encoding="utf-8")

    class _PerFileParser(FileParser):
        name = "perfile"
        extensions = (".tst",)
        PARSER_PARAMS = ()
        default_field_matching_policy = "positional"

        def parse_file(self, path: Path, *, table_name_hint=None) -> ParsedFile:
            if path.name == "a.tst":
                rows = [{"A": "1", "B": "foo", "C": "10"}]
                cols = ["A", "B", "C"]
            else:
                rows = [{"X": "2", "Y": "bar", "Z": "20"}]
                cols = ["X", "Y", "Z"]
            return ParsedFile(
                rows=pl.DataFrame(rows, schema={c: pl.String for c in cols}).lazy(),
                schema=ParserSchema(column_names=list(cols), column_types=None),
            )

    parser = _PerFileParser()
    df = parser.read(
        [file_a, file_b], contract_fields=[("id", None), ("name", None), ("price", None)],
    ).frame.collect().sort("__row_index__", "__source_file__")
    # Both files unified under contract names with values in the right cols.
    assert set(["id", "name", "price"]) <= set(df.columns)
    assert sorted(df["id"].to_list()) == ["1", "2"]
    assert sorted(df["name"].to_list()) == ["bar", "foo"]
    assert sorted(df["price"].to_list()) == ["10", "20"]
