from __future__ import annotations

import re

_PUNCT_TRAIL = re.compile(r"[?:.\s]+$")
_WS = re.compile(r"\s+")


def normalize(s: str | None) -> str:
    if s is None:
        return ""
    out = str(s).strip().lower()
    out = _PUNCT_TRAIL.sub("", out)
    out = _WS.sub(" ", out)
    return out


def find_column(headers: list[str | None], target: str) -> int | None:
    """Return the index of the column whose normalized header matches `target`, or None."""
    target_norm = normalize(target)
    if not target_norm:
        return None
    for idx, h in enumerate(headers):
        if normalize(h) == target_norm:
            return idx
    return None
