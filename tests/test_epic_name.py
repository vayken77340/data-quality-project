"""Tests for the epic-name validator (shared between CLI and runner)."""

from __future__ import annotations

import pytest

from data_contract._util import InvalidEpicName, validate_epic_name


# ---------------------------------------------------------------------------
# Accepted forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "1118",
    "1118_MVP",
    "1118 MVP",
    "1118 P1",
    "1118.0",
    "customer_360",
    "customer-360",
    "acme-q3",
    "a",            # single char (must start and end with alphanumeric)
    "Z9",
    "epic with multiple spaces inside",
])
def test_validate_epic_name_accepts_valid_names(name):
    assert validate_epic_name(name) == name


# ---------------------------------------------------------------------------
# Rejected forms -- one parametrized test per category so a failure points
# at the specific rule that broke.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_or_whitespace_rejected(bad):
    with pytest.raises(InvalidEpicName):
        validate_epic_name(bad)


@pytest.mark.parametrize("bad", [
    "../escape",
    "..",
    "1118/..",
    "foo..bar",      # `..` substring even without slash
])
def test_path_traversal_rejected(bad):
    with pytest.raises(InvalidEpicName, match="'..'"):
        validate_epic_name(bad)


@pytest.mark.parametrize("bad,char", [
    ("1118/MVP", "/"),
    ("1118\\MVP", "\\"),
    ("1118:MVP", ":"),
    ("1118?MVP", "?"),
    ("1118*MVP", "*"),
    ('1118"MVP', '"'),
    ("1118<MVP", "<"),
    ("1118>MVP", ">"),
    ("1118|MVP", "|"),
])
def test_windows_forbidden_chars_rejected(bad, char):
    with pytest.raises(InvalidEpicName, match="forbidden"):
        validate_epic_name(bad)


@pytest.mark.parametrize("bad", [
    " leading-space",
    "trailing-space ",
    " both ",
])
def test_leading_or_trailing_whitespace_rejected(bad):
    with pytest.raises(InvalidEpicName, match="whitespace"):
        validate_epic_name(bad)


@pytest.mark.parametrize("bad", [
    ".leading-dot",
    "-leading-hyphen",
    " leading-space",         # also caught by the whitespace rule
    "trailing-dot.",
    "trailing-hyphen-",
])
def test_must_start_and_end_with_alphanumeric(bad):
    with pytest.raises(InvalidEpicName):
        validate_epic_name(bad)


def test_single_dot_rejected():
    with pytest.raises(InvalidEpicName, match="reserved"):
        validate_epic_name(".")


def test_length_limit_enforced():
    # Boundary: 64 chars exactly is ok; 65 is not.
    name_ok = "a" * 64
    assert validate_epic_name(name_ok) == name_ok
    with pytest.raises(InvalidEpicName, match="64 characters"):
        validate_epic_name("a" * 65)


def test_unicode_letters_rejected():
    """Unicode normalization on Windows/macOS surprises iterators; restrict
    to ASCII to keep folder-name lookups portable."""
    with pytest.raises(InvalidEpicName):
        validate_epic_name("é1118")


def test_control_characters_rejected():
    with pytest.raises(InvalidEpicName):
        validate_epic_name("1118\x00MVP")
    with pytest.raises(InvalidEpicName):
        validate_epic_name("1118\nMVP")


def test_non_string_rejected():
    with pytest.raises(InvalidEpicName):
        validate_epic_name(1118)   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Error carries the offending name for downstream logging.
# ---------------------------------------------------------------------------


def test_exception_carries_epic_attribute():
    with pytest.raises(InvalidEpicName) as exc_info:
        validate_epic_name("1118/MVP")
    assert exc_info.value.epic == "1118/MVP"
