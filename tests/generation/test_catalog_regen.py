from __future__ import annotations

from pathlib import Path

from data_contract.generation.catalog import (
    DEFAULT_CONSTRAINTS_DOC,
    regen_constraint_doc,
    render_constraint_catalog_table,
    render_format_token_table,
    render_full_doc,
    would_regen_change,
)
from dq_core.field_constraints import REGISTRY
from dq_core.field_constraints.format import FORMAT_REGISTRY


def test_catalog_table_lists_every_registered_constraint():
    table = render_constraint_catalog_table()
    for name in REGISTRY:
        assert f"`{name}`" in table, f"constraint {name!r} missing from catalog"


def test_catalog_extracts_docstring_headers():
    """Each row should carry text from the constraint's docstring Spec cell /
    Contract output / Drift labels."""
    table = render_constraint_catalog_table()
    # min_value's docstring mentions parse_typed_value in Spec cell; should appear.
    assert "parse_typed_value" in table
    # unique should mention OUI/NON or "boolean token" (from its docstring).
    assert "OUI/NON" in table or "boolean token" in table


def test_format_token_table_lists_all_registered_formats():
    table = render_format_token_table()
    for token in FORMAT_REGISTRY:
        assert f"`{token}`" in table


def test_regen_is_idempotent_on_green(repo_root: Path):
    """The committed docs/constraints.md should already be in sync with the
    registry — second call returns False (no change)."""
    target = repo_root / "docs" / "constraints.md"
    # Sanity: first call may or may not change it; second must NOT.
    regen_constraint_doc(target)
    assert regen_constraint_doc(target) is False


def test_would_regen_detects_drift(tmp_path: Path, repo_root: Path):
    """Mutate the catalog block in a tmp copy, expect would_regen_change to flag it."""
    src = (repo_root / "docs" / "constraints.md").read_text(encoding="utf-8")
    target = tmp_path / "constraints.md"
    # Mangle the auto-generated catalog table.
    mangled = src.replace("| `unique` |", "| `STALE` |", 1)
    target.write_text(mangled, encoding="utf-8")
    assert would_regen_change(target) is True


def test_render_full_doc_preserves_prose_around_fences(repo_root: Path):
    src = (repo_root / "docs" / "constraints.md").read_text(encoding="utf-8")
    rendered = render_full_doc(src)
    # Prose section headers must survive untouched.
    assert "## 2. Sub-block conventions" in rendered
    assert "## 4. Docstring requirements" in rendered
    assert "## 5. How to add a constraint" in rendered


def test_lint_fails_when_catalog_is_stale(tmp_path: Path, repo_root: Path, monkeypatch):
    """End-to-end: monkeypatch DEFAULT_CONSTRAINTS_DOC to a mangled tmp file
    and confirm `lint --epic 1118` exits 2 with the catalog complaint."""
    from data_contract import cli

    src = (repo_root / "docs" / "constraints.md").read_text(encoding="utf-8")
    stale_path = tmp_path / "constraints.md"
    stale_path.write_text(src.replace("| `unique` |", "| `STALE` |", 1), encoding="utf-8")

    monkeypatch.setattr(cli, "DEFAULT_CONSTRAINTS_DOC", stale_path)
    monkeypatch.chdir(repo_root)
    rc = cli.main(["lint", "--epic", "1118"])
    assert rc == 2


def test_regen_docs_cli_returns_zero(tmp_path: Path, repo_root: Path, monkeypatch):
    from data_contract.cli import main

    src = (repo_root / "docs" / "constraints.md").read_text(encoding="utf-8")
    target = tmp_path / "constraints.md"
    target.write_text(src, encoding="utf-8")
    rc = main(["regen-docs", "--path", str(target)])
    assert rc == 0
