"""Auto-generated catalog blocks for `docs/constraints.md`.

Replaces the hand-maintained table at the top of the file. Two HTML-comment
fenced regions are owned by this module:

  <!-- BEGIN AUTO-GENERATED CATALOG --> ... <!-- END AUTO-GENERATED CATALOG -->
  <!-- BEGIN AUTO-GENERATED FORMATS --> ... <!-- END AUTO-GENERATED FORMATS -->

The catalog block is rendered from REGISTRY + each constraint's docstring
three-header convention. The formats block is rendered from FORMAT_REGISTRY.

Two CLI entry points consume this:
  - `lint` exits with a failure if regen would change the file (CI gate).
  - `regen-docs` rewrites in place (the developer's "fix it" command).
"""

from __future__ import annotations

import re
from pathlib import Path

from data_contract.field_constraints import REGISTRY
from data_contract.field_constraints.format import FORMAT_REGISTRY


DEFAULT_CONSTRAINTS_DOC = Path("docs/constraints.md")

CATALOG_BEGIN = "<!-- BEGIN AUTO-GENERATED CATALOG -->"
CATALOG_END = "<!-- END AUTO-GENERATED CATALOG -->"
FORMATS_BEGIN = "<!-- BEGIN AUTO-GENERATED FORMATS -->"
FORMATS_END = "<!-- END AUTO-GENERATED FORMATS -->"


def render_constraint_catalog_table() -> str:
    """Markdown table derived from REGISTRY + constraint docstrings."""
    rows: list[str] = [
        "| Name | Contract key | Spec cell | Contract output | Drift |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(REGISTRY):
        cls = REGISTRY[name]
        doc = cls.__doc__ or ""
        spec_cell = _extract_doc_field(doc, "Spec cell")
        contract_output = _extract_doc_field(doc, "Contract output")
        drift = _extract_doc_field(doc, "Drift")
        rows.append(f"| `{cls.name}` | `{cls.contract_key}` | {spec_cell} | {contract_output} | {drift} |")
    return "\n".join(rows)


def render_format_token_table() -> str:
    """Markdown table derived from FORMAT_REGISTRY."""
    rows: list[str] = [
        "| Token | Description |",
        "|---|---|",
    ]
    for name in sorted(FORMAT_REGISTRY):
        token = FORMAT_REGISTRY[name]
        rows.append(f"| `{token.name}` | {token.description} |")
    return "\n".join(rows)


def render_full_doc(current_text: str) -> str:
    """Return the text with both fenced regions replaced by fresh renders."""
    text = _replace_block(current_text, CATALOG_BEGIN, CATALOG_END, render_constraint_catalog_table())
    text = _replace_block(text, FORMATS_BEGIN, FORMATS_END, render_format_token_table())
    return text


def regen_constraint_doc(path: Path = DEFAULT_CONSTRAINTS_DOC) -> bool:
    """Rewrite `path` in place. Returns True iff the file content changed.

    Missing fence pairs are tolerated: the corresponding block is left untouched.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    current = path.read_text(encoding="utf-8")
    rendered = render_full_doc(current)
    if current == rendered:
        return False
    path.write_text(rendered, encoding="utf-8")
    return True


def would_regen_change(path: Path = DEFAULT_CONSTRAINTS_DOC) -> bool:
    """Read-only check for the lint gate. Same answer as `regen_constraint_doc`
    would give, without touching the file."""
    if not path.is_file():
        return False
    current = path.read_text(encoding="utf-8")
    return current != render_full_doc(current)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DOC_FIELD_RE = re.compile(r"^\s*(?P<label>[A-Z][A-Za-z ]+):\s*(?P<value>.+)$")


def _extract_doc_field(doc: str, label: str) -> str:
    """Extract the (possibly multi-line continuation) value following `<label>:`
    in a constraint docstring. Continuation lines are recognized by indentation
    that exceeds the label line's indent and lack of their own `Xxx:` prefix."""
    lines = doc.splitlines()
    collected: list[str] = []
    capturing = False
    label_indent = 0
    for line in lines:
        m = _DOC_FIELD_RE.match(line)
        if m and m.group("label").strip().lower() == label.lower():
            collected.append(m.group("value").strip())
            capturing = True
            label_indent = len(line) - len(line.lstrip())
            continue
        if capturing:
            stripped = line.strip()
            if not stripped:
                break
            this_indent = len(line) - len(line.lstrip())
            # Stop on another `Xxxx:` header at <= the label's indent.
            if _DOC_FIELD_RE.match(line) and this_indent <= label_indent:
                break
            collected.append(stripped)
    return " ".join(collected).strip()


def _replace_block(text: str, begin: str, end: str, content: str) -> str:
    """Replace the inclusive content between `begin` and `end` markers.
    If either marker is missing, return text unchanged.
    """
    begin_idx = text.find(begin)
    end_idx = text.find(end)
    if begin_idx < 0 or end_idx < 0 or end_idx < begin_idx:
        return text
    # Preserve a blank line after BEGIN and before END for readability.
    new_block = f"{begin}\n\n{content}\n\n{end}"
    return text[:begin_idx] + new_block + text[end_idx + len(end):]
