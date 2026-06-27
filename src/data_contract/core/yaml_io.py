"""YAML I/O helpers used by every config loader.

Single source of truth for:
  - canonical dump formatting (UTF-8, LF, no flow style, no key-sorting)
  - permissive load (missing/empty file -> {})
  - load + require-mapping (raises ConfigError on non-dict top level)

Replaces the inline `yaml.safe_load(path.read_text(...))` plus the
duplicated "top-level YAML must be a mapping" error message that used
to live in seven modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError


class FlowList(list):
    """A list subclass that renders inline (flow style) in dumped YAML.

    Used for short token lists (e.g. `data_values` boolean tokens) where
    one-line representation is more readable than the default block style.
    """


def _flow_list_representer(dumper: yaml.SafeDumper, data: FlowList):
    return dumper.represent_sequence(
        "tag:yaml.org,2002:seq", list(data), flow_style=True,
    )


yaml.SafeDumper.add_representer(FlowList, _flow_list_representer)


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning an empty dict on missing/empty payload.

    Non-mapping payloads (a YAML list at top level, a bare scalar) also
    return {}. Callers that need to reject those shapes should use
    `load_yaml_mapping` instead.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def load_yaml_mapping(path: Path, *, what: str = "file") -> dict[str, Any]:
    """Load a YAML file that MUST have a mapping at the top level.

    `what` is interpolated into the error message ("validation config",
    "type registry", ...). Missing file is the caller's problem -- raise
    upfront with `path.is_file()` if needed.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping ({what})")
    return raw


def dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write a YAML file with the project's canonical formatting.

    Adds blank lines between top-level list items that span multiple lines
    (so per-field contract entries don't run into each other), and between
    a top-level scalar block and the next mapping/list block. Single-line
    siblings stay packed.
    """
    rendered = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    pretty = _add_blank_line_separators(rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(pretty)


def _add_blank_line_separators(text: str) -> str:
    """Insert blank lines for readability without changing the YAML semantics.

    Three rules:
      1. Before each top-level list item (`- ` at column 0) whose content
         spans more than one line -- keeps `fields:` entries visually
         separated without padding short scalar lists.
      2. Before each top-level mapping key whose value is itself indented
         on the next line (a nested mapping or list) -- separates the
         metadata block from `spec:`, `fields:`, etc.
      3. After a top-level nested block ends: before a top-level scalar
         entry whose previous line is still indented (i.e. the nested
         block just closed). Stops `spec:`'s last `sheet_name:` line from
         running straight into `table:` on the next line.

    Idempotent: re-running on already-spaced output adds nothing.
    """
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not line or out and out[-1] == "":
            out.append(line)
            continue
        # Don't insert a blank right after the parent key (`fields:` directly
        # followed by `- name:`); only between sibling items.
        prev_is_key_header = bool(out) and out[-1].rstrip().endswith(":")
        prev_is_indented = bool(out) and out[-1].startswith(" ")
        if i > 0 and not prev_is_key_header and _starts_top_level_list_item(line, lines, i):
            out.append("")
        elif i > 0 and _starts_top_level_nested_block(line, lines, i):
            out.append("")
        elif i > 0 and prev_is_indented and _is_top_level_scalar(line):
            out.append("")
        out.append(line)
    if out and out[-1] != "":
        out.append("")
    return "\n".join(out)


def _starts_top_level_list_item(line: str, lines: list[str], i: int) -> bool:
    """True iff `line` is `- ...` at column 0 AND has at least one indented
    continuation line beneath it (i.e. the item is multi-line)."""
    if not line.startswith("- "):
        return False
    return i + 1 < len(lines) and lines[i + 1].startswith("  ")


def _starts_top_level_nested_block(line: str, lines: list[str], i: int) -> bool:
    """True iff `line` is a top-level key whose value is on subsequent
    indented lines (`key:` followed by `  ...` or `- ...`)."""
    if not line or line[0] in " -":
        return False
    if not line.rstrip().endswith(":"):
        return False
    if i + 1 >= len(lines):
        return False
    nxt = lines[i + 1]
    return nxt.startswith("  ") or nxt.startswith("- ")


def _is_top_level_scalar(line: str) -> bool:
    """True iff `line` is a top-level `key: value` pair where the value is
    inline on the same line (not a nested block header)."""
    if not line or line[0] in " -":
        return False
    stripped = line.rstrip()
    if ":" not in stripped:
        return False
    return not stripped.endswith(":")
