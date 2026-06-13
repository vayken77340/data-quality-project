"""Tests for the business-facing HTML report."""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import yaml

from data_contract.cli import main
from tests.conftest import ALL_CHECKS_ENABLED_YAML


def _build_epic(tmp_path: Path, *, target=None) -> Path:
    epic = tmp_path / "epics" / "T"
    (epic / "configs").mkdir(parents=True)
    (epic / "contracts").mkdir()
    (epic / "sample").mkdir()
    repo = Path(__file__).resolve().parents[2]
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "types.yaml").write_text(
        (repo / "configs" / "types.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "configs" / "targets").mkdir()
    for n in ("oracle.yaml", "postgres.yaml", "iceberg.yaml"):
        (tmp_path / "configs" / "targets" / n).write_text(
            (repo / "configs" / "targets" / n).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    contract = {
        "version": "1.0", "epic": "T", "table": "T",
        "source": {"spec_file": "s", "spec_sheet": "T"},
        "fields": [
            {"name": "id", "type": "int64", "nullable": False, "primary_key": True},
            {"name": "label", "type": "string", "nullable": False, "max_length": 3},
        ],
    }
    (epic / "contracts" / "T.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8",
    )
    target_line = f"target: {target or 'postgres'}\n"
    (epic / "configs" / "validation.yaml").write_text(
        ALL_CHECKS_ENABLED_YAML + target_line +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "sample/{table}.csv"
        """),
        encoding="utf-8",
    )
    return epic


def _run(tmp_path: Path, csv_content: str, *, target=None) -> str:
    epic = _build_epic(tmp_path, target=target)
    (epic / "sample" / "T.csv").write_text(csv_content, encoding="utf-8")
    out = tmp_path / "out"
    main([
        "validate-data", "--epic", "T",
        "--epic-root", str(tmp_path / "epics"),
        "--input-dir", str(epic),
        "--output-dir", str(out),
        "--types", str(tmp_path / "configs" / "types.yaml"),
    ])
    return (out / "quality_report.html").read_text(encoding="utf-8")


def test_html_renders_for_clean_run(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,abc\n")
    assert "<!DOCTYPE html>" in html
    assert "data quality report" in html.lower()
    assert "100.0 / 100" in html
    assert "PASS" in html


def test_html_renders_for_failing_run(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,toolong\n")
    assert "FAIL" in html
    assert "max_length_violation" in html
    # Dimension pill rendered.
    assert "validity" in html


def test_html_is_self_contained(tmp_path: Path):
    """No external scripts / stylesheets / images -- the file is portable."""
    html = _run(tmp_path, "id,label\n1,abc\n")
    assert not re.search(r"<link\s", html, re.IGNORECASE), "external <link> tag found"
    assert not re.search(r"<script\b", html, re.IGNORECASE), "external <script> tag found"
    assert not re.search(r'src="https?://', html, re.IGNORECASE), "external src= found"
    assert not re.search(r'href="https?://', html, re.IGNORECASE), "external href= found"


def test_html_dimension_panel_present(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,abc\n")
    for dim in ("completeness", "validity", "uniqueness", "consistency"):
        assert dim in html.lower()


def test_html_surfaces_target(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,abc\n", target="postgres")
    assert "postgres" in html.lower()


def test_html_rejected_rows_compact_pk_plus_violation_table(tmp_path: Path):
    """Rejected-rows section shows PK + a tight per-violation table (Column /
    Value / Check / Expected / Why) rather than dumping the full source row."""
    html = _run(tmp_path, "id,label\n1,toolong\n")
    assert "Rejected rows" in html
    # PK line carries the primary key, not the full source-row dump.
    assert "id=1" in html
    assert "label=toolong" not in html, "old full-source-row dump should be gone"
    # Violation table headers + the offending value and friendly check label.
    assert "<table class=\"violations\">" in html
    assert ">Column<" in html and ">Value<" in html and ">Check<" in html
    assert ">Expected<" in html and ">Why<" in html
    assert "toolong" in html               # offending value
    assert "Value too long" in html        # friendly check label
    assert "max 3 chars" in html           # terse Expected
    # Hint appears in the violation table.
    assert "shorten" in html.lower() or "raise the cap" in html.lower()


def test_html_top_issues_drill_down_lists_top_values(tmp_path: Path):
    """Top issues are expandable; each card lists value+count for the
    offenders that triggered the check."""
    html = _run(tmp_path, "id,label\n1,toolong\n2,toolong\n3,short\n4,alsotoolong\n")
    # `<details class="issue">` wrapper used for the drill-down.
    assert "<details class=\"issue\">" in html
    # Top values table renders with the offending strings + their counts.
    assert "top-values" in html
    assert "toolong" in html
    # Friendly check label is used in the summary, not the raw kind.
    assert "Value too long" in html


def test_html_profile_section_present(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,abc\n2,def\n")
    assert "Data profile" in html
    # New column headers (full names).
    assert "Primary Key" in html
    assert "Foreign Key" in html
    assert "Type Format" in html


def test_html_checks_section_lists_enabled_checks(tmp_path: Path):
    """The Checks section (renamed from Hints) shows enabled checks with
    descriptions and violation kinds."""
    html = _run(tmp_path, "id,label\n1,abc\n")
    assert ">Checks<" in html
    # New layout has Check / Description / Violation kind columns.
    assert "Violation kind" in html
    # Sample enabled check + its violation kind.
    assert "type_coercion" in html
    assert "type_coercion_violation" in html


def test_html_table_issues_section_surfaces_column_missing(tmp_path: Path):
    """A table-level violation (column_missing) shows up in the Table issues
    section with friendly check label, field, expected, and hint."""
    html = _run(tmp_path, "id\n1\n")          # CSV missing the `label` column
    assert "<h2>Table issues</h2>" in html
    assert "Column missing in source" in html
    # `label` field name appears in the table.
    assert "label" in html
    # Action hint is shown.
    assert "Add the column" in html or "remove the field" in html.lower()


def test_html_run_metadata_footer(tmp_path: Path):
    html = _run(tmp_path, "id,label\n1,abc\n")
    assert "Tool version" in html
    assert "Checks enabled" in html
    assert "Checks disabled" in html
    assert "CLI args" in html
