"""Tests for the engineer-facing Markdown report."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import yaml

from data_contract.cli import main
from tests.conftest import ALL_CHECKS_ENABLED_YAML


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
    # Targets always needed now (target: is required in validation.yaml).
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

    # Build a checks block honouring `disable` overrides. Shorthand
    # `name: true|false` form (descriptions live in report_strings.yaml).
    checks_lines = ["checks:"]
    base = {
        "type_coercion": True, "boolean_coercion": True, "nullable": True,
        "max_length": True, "column_missing": True, "pk_uniqueness": True,
        "fk_existence": True, "allowed_values": True, "pattern": True,
        "min_value": True, "max_value": True, "format": True, "unique": True,
    }
    base.update(disable or {})
    for name, v in base.items():
        checks_lines.append(f"  {name}: {str(v).lower()}")
    checks_block = "\n".join(checks_lines) + "\n"

    target_line = f"target: {target or 'postgres'}\n"
    (epic / "configs" / "validation.yaml").write_text(
        checks_block + target_line +
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
    assert "max_length_violation" in md
    # Hint must be in the rendered top-issue line.
    assert "max_length" in md.lower()
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
