"""Epic-name validation + epic-directory discovery.

Epic names flow into filesystem paths (epics/<epic>/...), into every
generated contract's `epic:` field, and into report output paths. We
accept a permissive charset that covers the common conventions
("1118", "1118_MVP", "1118 P1", "customer-360") but reject anything
that could escape the epic root, exploit platform path semantics, or
silently match the wrong directory.
"""

from __future__ import annotations

import re
from pathlib import Path


# Reserved on Windows (NTFS / Win32 path API). Linux/macOS accept most of
# these, but we reject them universally so an epic created on Linux can
# still be opened on Windows. Forward slash and backslash are obvious
# path separators; the others are the Win32 reserved set.
_EPIC_FORBIDDEN_CHARS = frozenset('/\\:*?"<>|')

# Allowed character class: ASCII letters/digits, space, dot, underscore,
# hyphen. Unicode letters are deliberately excluded so the on-disk folder
# name is portable (Windows + macOS HFS+ both normalize Unicode in ways
# that surprise scripts iterating with glob).
_EPIC_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._\-]+$")


class InvalidEpicName(ValueError):
    """Raised when an epic name fails validation. Carries the offending
    name in `.epic` and the human-readable rule in `args[0]`."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(
            f"invalid epic name {name!r}: {reason}. "
            f"Rules: 1-64 chars; letters, digits, spaces, '.', '_', '-' only; "
            f"must start and end with a letter or digit; no '..'."
        )
        self.epic = name


def validate_epic_name(name: str) -> str:
    """Return `name` if it's a valid epic identifier; raise `InvalidEpicName`
    otherwise.

    Accepted: 1-64 character ASCII string of letters/digits/spaces and the
    three punctuation characters `.`, `_`, `-`. Must start AND end with a
    letter or digit.
    """
    if not isinstance(name, str):
        raise InvalidEpicName(str(name), "must be a string")
    if not name:
        raise InvalidEpicName(name, "must not be empty")
    if len(name) > 64:
        raise InvalidEpicName(name, "must be 64 characters or fewer")
    if name != name.strip():
        raise InvalidEpicName(name, "must not start or end with whitespace")
    if name in (".", ".."):
        raise InvalidEpicName(name, "reserved")
    if ".." in name:
        raise InvalidEpicName(name, "must not contain '..'")
    bad = sorted(set(name) & _EPIC_FORBIDDEN_CHARS)
    if bad:
        raise InvalidEpicName(name, f"contains forbidden character(s): {bad}")
    if not _EPIC_ALLOWED_RE.match(name):
        raise InvalidEpicName(
            name,
            "contains characters outside the allowed set "
            "(letters, digits, space, '.', '_', '-')",
        )
    if not (name[0].isalnum() and name[-1].isalnum()):
        raise InvalidEpicName(
            name,
            "must start and end with a letter or digit",
        )
    return name


def discover_epics(epic_root: Path, *, required_subdir: str = "configs") -> list[str]:
    """List every epic directory under `epic_root` that has the named subdir.

    `required_subdir` defaults to `configs/` (the generation-side gate) but
    `validate-contract` passes `contracts/` so it only sees epics whose
    contracts have already been built.

    Returns names sorted alphabetically. Empty list when the root is
    missing — callers decide whether that's a hard error.
    """
    if not epic_root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(epic_root.iterdir()):
        if child.is_dir() and (child / required_subdir).is_dir():
            out.append(child.name)
    return out
