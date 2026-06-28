from __future__ import annotations

import pytest

from dq_core.slugify import slugify


@pytest.mark.parametrize("raw, expected", [
    # Already-clean inputs are idempotent.
    ("reference_number", "reference_number"),
    ("customer_email", "customer_email"),
    # Spaces -> underscore.
    ("Reference Number", "reference_number"),
    ("Customer Email Address", "customer_email_address"),
    # Punctuation runs collapse.
    ("Order #", "order"),
    ("Customer (Primary)", "customer_primary"),
    ("first.last", "first_last"),
    # Accents and diacritics strip via NFKD.
    ("Date d'envoi", "date_d_envoi"),
    ("N° de commande", "n_de_commande"),
    ("Résumé", "resume"),
    ("naïve", "naive"),
    # Leading digits get a `_` prefix so they're valid SQL identifiers.
    ("2024 Revenue", "_2024_revenue"),
    ("12 Month Trend", "_12_month_trend"),
    # Acronyms separated by whitespace slug together (URL stays urlish), but
    # adjacent acronym + word with no separator concatenates -- that's where
    # spec authors override via Nom BDD.
    ("Customer URL", "customer_url"),
    ("API Key", "api_key"),
    ("CustomerID", "customerid"),  # no separator -> no split; override if needed
    # Edge cases.
    ("", ""),
    ("   ", ""),
    ("___", ""),
    ("!!!", ""),
])
def test_slugify_rules(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_is_idempotent() -> None:
    """Running slugify twice yields the same result -- safe to apply in
    pipelines that re-derive names from already-processed values."""
    for sample in ("Reference Number", "Date d'envoi", "2024 Revenue"):
        once = slugify(sample)
        twice = slugify(once)
        assert once == twice


def test_slugify_non_latin_degrades_to_underscore() -> None:
    """NFKD strips combining marks but cannot transliterate non-Latin
    scripts. Documented behaviour -- ingesting CJK/Cyrillic specs would
    need a `unidecode` swap."""
    assert slugify("データ") == ""        # JP "deta"
    assert slugify("Mixed 中文 Latin") == "mixed_latin"
