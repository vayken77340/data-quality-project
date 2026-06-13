"""Tests for the action-hint registry."""

from __future__ import annotations

import pytest

from data_contract.validation.report.dimensions import _KIND_TO_DIMENSION
from data_contract.validation.report.hints import HINTS, hint_for


def test_every_kind_has_a_hint():
    """Every kind that has a dimension must also have a hint -- the two
    registries are kept in lockstep so reports never show a violation with
    no actionable guidance."""
    missing = sorted(set(_KIND_TO_DIMENSION) - set(HINTS))
    assert missing == [], f"kinds missing hints: {missing}"


def test_every_hint_has_a_dimension():
    """Symmetric: a hint with no dimension would never surface."""
    orphan = sorted(set(HINTS) - set(_KIND_TO_DIMENSION))
    assert orphan == [], f"hints with no dimension mapping: {orphan}"


def test_hint_for_known_kind():
    text = hint_for("nullable_violation")
    assert "required" in text.lower()


def test_hint_for_unknown_raises():
    with pytest.raises(KeyError, match="unknown violation kind"):
        hint_for("never_heard_of_it")


def test_hints_are_non_empty_plain_english():
    for kind, hint in HINTS.items():
        assert isinstance(hint, str)
        assert hint.strip(), f"{kind!r} hint is empty"
        # No regex-looking jargon in the user-facing hint.
        assert "\\d" not in hint, f"{kind!r} hint contains regex syntax"
        assert "^" not in hint or "Y/N" in hint, f"{kind!r} hint contains regex syntax"
        # Reasonable length for a single-sentence action.
        assert 20 <= len(hint) <= 400, f"{kind!r} hint length {len(hint)} out of expected range"
