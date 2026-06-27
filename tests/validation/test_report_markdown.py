"""Tests for the engineer-facing Markdown report."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import yaml

from data_contract.cli import main
from tests.conftest import ALL_CHECKS_ENABLED_YAML, write_test_parsers_yaml


def _build_epic(tmp_path: Path, *, target: str | None = None,
                disable: dict[str, bool] | None = None) -> Path:
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
    write_test_parsers_yaml(tmp_path / "configs")
    # Targets always needed now (target: is required in validation.yaml).
    (tmp_path / "configs" / "targets").mkdir()
    for n in ("oracle.yaml", "postgres.yaml", "iceberg.yaml"):
        (tmp_path / "configs" / "targets" / n).write_text(
            (repo / "configs" / "targets" / n).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    contract = {
        "version": "1.0", "epic": "T", "table": "T",
        "target": target or "postgres",
        "spec": {"file_path": "s", "sheet_name": "T"},
        "fields": [
            {"name": "id", "type": "int64", "nullable": False, "primary_key": True},
            {"name": "label", "type": "string", "nullable": False, "max_length": 3},
        ],
    }
    (epic / "contracts" / "T.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8",
    )

    # Build a tier-keyed checks + metrics block honouring `disable` overrides.
    base = {
        "type_coercion": True, "boolean_coercion": True, "nullable": True,
        "max_length": True, "column_missing": True, "pk_uniqueness": True,
        "fk_existence": True, "allowed_values": True, "pattern": True,
        "min_value": True, "max_value": True, "format": True, "unique": True,
        "field_names_from_sample": False, "field_types_from_sample": False,
    }
    base.update(disable or {})
    tier_of = {
        "type_coercion": "structural", "boolean_coercion": "structural",
        "nullable": "structural", "max_length": "structural",
        "field_names_from_sample": "structural", "field_types_from_sample": "structural",
        "column_missing": "table", "pk_uniqueness": "table", "fk_existence": "table",
        "allowed_values": "field", "pattern": "field", "min_value": "field",
        "max_value": "field", "format": "field", "unique": "field",
    }
    by_tier: dict[str, list[tuple[str, bool]]] = {"structural": [], "table": [], "field": []}
    for name, v in base.items():
        by_tier[tier_of[name]].append((name, v))
    checks_lines = ["checks:"]
    for tier in ("structural", "table", "field"):
        checks_lines.append(f"  {tier}:")
        for name, v in by_tier[tier]:
            checks_lines.append(f"    {name}: {str(v).lower()}")
    checks_lines.append("metrics:")
    checks_lines.append("  field:")
    for n in ("null_count", "null_percentage", "distinct_count", "completeness", "duplicate_pct"):
        checks_lines.append(f"    {n}: true")
    checks_lines.append("  table:")
    checks_lines.append("    row_count: true")
    checks_block = "\n".join(checks_lines) + "\n"

    (epic / "configs" / "validation.yaml").write_text(
        checks_block +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "sample/{table}.csv"
        """),
        encoding="utf-8",
    )
    return epic


def _run(tmp_path: Path, csv_content: str, *, target=None, disable=None) -> str:
    epic = _build_epic(tmp_path, target=target, disable=disable)
    (epic / "sample" / "T.csv").write_text(csv_content, encoding="utf-8")
    out = tmp_path / "out"
    main([
        "validate-data", "--epic", "T",
        "--epic-root", str(tmp_path / "epics"),
        "--input-dir", str(epic),
        "--output-dir", str(out),
        "--types", str(tmp_path / "configs" / "types.yaml"),
    ])
    return (out / "quality_report.md").read_text(encoding="utf-8")


def test_md_clean_run_shows_pass(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,abc\n2,def\n")
    assert "**PASS**" in md
    assert "100.0 / 100" in md
    assert "all checks clean" in md.lower()


def test_md_fail_run_shows_fail_and_top_issue(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,toolong\n")
    assert "**FAIL**" in md
    # Top issues use the business-friendly label, not the raw kind string.
    assert "Value too long" in md
    # Detail block carries the contract's max_length AND the longest actual length.
    assert "limit=3" in md         # contract says max_length=3
    assert "longest actual=7" in md  # 'toolong' is 7 chars
    # The offending value itself shows up in the sample.
    assert "toolong" in md
    # Hint sentence still rendered after the detail block.
    assert ("shorten" in md.lower() or "raise the cap" in md.lower())


def test_md_dimension_table_present(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,abc\n")
    assert "| Dimension | Score | Violations |" in md
    for dim in ("Completeness", "Validity", "Uniqueness", "Consistency"):
        assert dim in md


def test_md_per_table_table_present(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,abc\n")
    assert "## Tables" in md
    assert "| Table | Score | Rows | Clean | Errors | Warn |" in md


def test_md_surfaces_active_target(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,abc\n", target="postgres")
    assert "Target: postgres" in md


def test_md_surfaces_disabled_checks(tmp_path: Path):
    md = _run(tmp_path, "id,label\n1,abc\n", disable={"fk_existence": False})
    assert "fk_existence" in md
    assert "disabled" in md.lower()


def test_md_table_issues_section_surfaces_column_missing(tmp_path: Path):
    """`column_missing` (table-level) gets a dedicated `## Table issues`
    section above Top issues -- not buried in row-level aggregation."""
    md = _run(tmp_path, "id\n1\n")            # CSV missing the `label` column
    assert "## Table issues" in md
    # Section line carries severity, table, friendly check label, and field.
    assert "Column missing in source" in md
    assert "`label`" in md or "label" in md
    # Table issues appear BEFORE Top issues in the document.
    assert md.index("## Table issues") < md.index("## Top issues")
