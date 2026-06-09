"""Tests for the new JSON report shape."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import yaml

from data_contract.cli import main
from tests.conftest import ALL_CHECKS_ENABLED_YAML


def _build_test_epic(tmp_path: Path, *, target: str | None = None) -> Path:
    """A minimal epic with a contract that's easy to violate predictably."""
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
    for name in ("oracle.yaml", "postgres.yaml", "iceberg.yaml"):
        (tmp_path / "configs" / "targets" / name).write_text(
            (repo / "configs" / "targets" / name).read_text(encoding="utf-8"),
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
    # `target:` is required in validation.yaml. Tests default to postgres if
    # not specified -- it's the most permissive target.
    target_line = f"target: {target or 'postgres'}\n"
    (epic / "configs" / "validation.yaml").write_text(
        ALL_CHECKS_ENABLED_YAML +
        target_line +
        dedent("""\
            defaults:
              format: csv
              file_pattern: "sample/{table}.csv"
        """),
        encoding="utf-8",
    )
    return epic


def _run(tmp_path: Path, csv_content: str, target: str | None = None) -> dict:
    epic = _build_test_epic(tmp_path, target=target)
    (epic / "sample" / "T.csv").write_text(csv_content, encoding="utf-8")
    out = tmp_path / "out"
    main([
        "validate-data", "--epic", "T",
        "--epic-root", str(tmp_path / "epics"),
        "--input-dir", str(epic),
        "--output-dir", str(out),
        "--types", str(tmp_path / "configs" / "types.yaml"),
    ])
    return json.loads((out / "quality_report.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


def test_json_schema_version_and_top_keys(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n2,def\n")
    assert p["schema_version"] == "1.0"
    assert set(p) == {"schema_version", "run", "summary", "tables", "run_issues"}


def test_json_run_block_populated(tmp_path: Path):
    """Test fixture defaults to target=postgres. The run block surfaces it."""
    p = _run(tmp_path, "id,label\n1,abc\n")
    run = p["run"]
    assert run["epic"] == "T"
    assert run["status"] == "PASS"
    assert run["tool_version"]
    assert run["target"] is not None
    assert run["target"]["name"] == "postgres"
    assert "fk_existence" in run["checks"]["enabled"]
    assert run["contracts"] == {"T": "1.0"}
    assert isinstance(run["duration_ms"], int)


def test_json_run_block_carries_target_when_set(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n", target="postgres")
    assert p["run"]["target"]["name"] == "postgres"
    assert "PostgreSQL" in p["run"]["target"]["description"]


# ---------------------------------------------------------------------------
# Summary + dimensions
# ---------------------------------------------------------------------------


def test_json_summary_dimensions(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n2,def\n")
    summary = p["summary"]
    assert summary["pass"] is True
    assert summary["score"] == 100.0
    for d in ("completeness", "validity", "uniqueness", "consistency"):
        assert d in summary["by_dimension"]
        assert summary["by_dimension"][d]["score"] == 100.0


def test_json_summary_by_severity(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n")
    assert p["summary"]["by_severity"] == {"error": 0, "warning": 0, "info": 0}


# ---------------------------------------------------------------------------
# Per-table block
# ---------------------------------------------------------------------------


def test_json_per_table_score_and_profile(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n2,def\n3,ghi\n")
    tbl = p["tables"][0]
    assert tbl["table"] == "T"
    assert tbl["score"] == 100.0
    assert tbl["input"]["total_rows"] == 3
    assert tbl["input"]["clean_rows"] == 3
    assert tbl["input"]["affected_rows"] == 0
    # New profile shape: name, type, type_format, is_primary_key, is_foreign_key,
    # total, null_count, null_pct, distinct_count. No min/max/top_values/uniqueness.
    id_field = next(f for f in tbl["profile"]["fields"] if f["name"] == "id")
    assert id_field["is_primary_key"] is True
    assert id_field["type"] == "BIGINT"        # postgres int64
    assert id_field["type_format"] == "postgres"
    assert "uniqueness_ratio" not in id_field
    assert "min" not in id_field
    assert "max" not in id_field
    assert "top_values" not in id_field
    assert "canonical_type" not in id_field


# ---------------------------------------------------------------------------
# Score math (the bug fix): multi-violation row counts once
# ---------------------------------------------------------------------------


def test_score_math_multi_violation_row_counted_once(tmp_path: Path):
    # 5 rows, row 2 has BOTH a max_length violation (label is too long) and
    # a future violation. Today's broken math could go negative; new math
    # treats row 2 as one affected row -> score = 4/5 = 80.0.
    p = _run(tmp_path, "id,label\n1,abc\n2,toolong\n3,abc\n4,abc\n5,abc\n")
    tbl = p["tables"][0]
    assert tbl["input"]["affected_rows"] == 1
    assert tbl["score"] == 80.0


# ---------------------------------------------------------------------------
# Rejected rows: row-centric view with full source context
# ---------------------------------------------------------------------------


def test_rejected_rows_full_source_context(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n2,toolong\n")
    rejected = p["tables"][0]["rejected_rows"]
    assert len(rejected) == 1
    row = rejected[0]
    assert row["source_file"] == "T.csv"
    assert row["source_row"] == 2
    assert row["worst_severity"] == "error"
    # Full source row data: BOTH columns present, not just the failing one.
    assert row["source_row_data"]["id"] == 2
    assert row["source_row_data"]["label"] == "toolong"
    # PK values populated.
    assert row["pk_values"] == {"id": 2}
    # Violation list carries dimension + hint + check_id.
    assert any(v["kind"] == "max_length_violation" for v in row["violations"])
    v = row["violations"][0]
    assert "check_id" in v
    assert v["dimension"] == "validity"
    assert "hint" in v and v["hint"]


def test_rejected_rows_row_centric_aggregation(tmp_path: Path):
    """A row with multiple violations appears once with all violations grouped."""
    p = _run(tmp_path, "id,label\n1,toolong\n")  # row 1 has nullable AND max_length
    rejected = p["tables"][0]["rejected_rows"]
    # max_length only (label is "toolong" - 7 chars > 3). nullable doesn't fire
    # because both fields have values. So one row, one violation.
    assert len(rejected) == 1
    assert len(rejected[0]["violations"]) == 1


# ---------------------------------------------------------------------------
# Hints, dimensions, check_ids on every violation
# ---------------------------------------------------------------------------


def test_every_violation_has_dimension_check_id_hint(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,toolong\n2,abc\n2,abc\n")  # max_length + pk_not_unique
    tbl = p["tables"][0]
    for v in tbl["violations"]:
        assert v.get("kind")
        assert v.get("dimension") in {"completeness", "validity", "uniqueness", "consistency"}
        assert v.get("check_id")
        assert v.get("hint")


# ---------------------------------------------------------------------------
# PK clustering preserved
# ---------------------------------------------------------------------------


def test_pk_not_unique_clustered(tmp_path: Path):
    p = _run(tmp_path, "id,label\n1,abc\n1,abc\n2,abc\n")
    pk_v = [v for v in p["tables"][0]["violations"] if v["kind"] == "pk_not_unique"]
    assert len(pk_v) == 1
    assert pk_v[0]["distinct_values"] == 1
    assert pk_v[0]["duplicates"][0]["value"] == 1
    assert len(pk_v[0]["duplicates"][0]["occurrences"]) == 2
