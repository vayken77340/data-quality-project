"""Business header to database-identifier normaliser.

`slugify("Reference Number") -> "reference_number"` -- the canonical step
the generator runs on each spec field name to derive a clean snake_case
database column name. Spec authors who want a different result for a
specific row can declare an explicit `Nom BDD` cell in the spec; the
builder uses that verbatim and skips slugify for that row.

Limits worth knowing:
  - Non-Latin scripts (CJK, Cyrillic, Arabic) decompose to empty under
    NFKD + combining-mark stripping; their slug is "". Acceptable for
    French/English specs; if a spec ever ingests non-Latin headers, add
    `unidecode` and swap the NFKD step for `unidecode.unidecode(...)`.
  - Adjacent words with no separator stay glued ("CustomerID" ->
    "customerid"). Spec authors override via `Nom BDD` for the handful
    of fields where the auto-slug isn't what the database team wants.
"""

from __future__ import annotations

import re
import unicodedata


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, strip accents, non-alphanumeric -> `_`, collapse repeats.

    Prepends `_` when the first character is a digit (databases reject
    identifiers like `2024_revenue`). Empty / whitespace-only input -> "".
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = _NON_ALNUM.sub("_", ascii_only.lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = "_" + slug
    return slug
