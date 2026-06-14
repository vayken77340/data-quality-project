"""Small cross-module helpers. Kept dependency-free so any module can import."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now_iso_z() -> str:
    """UTC timestamp in ISO-8601 form, second-precision, trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Epic-name validation
# ---------------------------------------------------------------------------
#
# Epic names flow into filesystem paths (epics/<epic>/...), into every
# generated contract's `epic:` field, and into report output paths. We
# accept a permissive charset that covers the common conventions
# ("1118", "1118_MVP", "1118 P1", "customer-360") but reject anything
# that could escape the epic root, exploit platform path semantics, or
# silently match the wrong directory.

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
    letter or digit (so no `--foo`, no `foo `, no leading dots).

    Rejected:
    - Empty or all-whitespace.
    - Containing any of `/`, `\\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`
      (Windows-illegal / path separators).
    - Containing `..` anywhere (path-traversal guard, even if there's no
      slash next to it).
    - Control characters or characters outside the allowed set.
    - Bare `.` or `..` (current/parent dir tokens).

    The function is conservative: every accepted name lands in a folder
    name that's portable across Windows, macOS, and Linux and survives a
    round-trip through the contract YAML's `epic:` field.
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
        # Anything not caught above is a control character or other
        # outside-the-charset glyph.
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


def dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write a YAML file with the project's canonical formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(
            payload,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning an empty dict on missing/empty payload."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}
