"""End-to-end integration tests for `validate-data` on epic 1118."""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import load_workbook

from data_contract.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "1118"


def _outdir(tmp_path: Path) -> Path:
    return tmp_path / "validations"


def test_clean_run_passes_and_emits_three_reports(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "clean"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    assert (out / "quality_report.xlsx").exists()
    assert (out / "quality_report.json").exists()
    assert (out / "quality_report.md").exists()

    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["pass"] is True
    assert payload["summary"]["errors"] == 0
    assert payload["tables"][0]["table"] == "PROJECT"


def test_dup_pk_fails_with_clustered_violations(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "dup_pk"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["pass"] is False

    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    pk_blocks = [v for v in project["violations"] if v["kind"] == "pk_not_unique"]
    assert len(pk_blocks) == 1
    block = pk_blocks[0]
    assert block["row_count"] == 2  # two participating rows
    assert block["distinct_values"] == 1
    occ = block["duplicates"][0]["occurrences"]
    assert {o["source_file"] for o in occ} == {"PROJECT.xlsx"}
    assert sorted(o["source_row"] for o in occ) == [1, 2]


def test_multi_file_cross_file_pk_collision(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "multi_file"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    assert project["input"]["total_rows"] == 4
    pk_blocks = [v for v in project["violations"] if v["kind"] == "pk_not_unique"]
    block = pk_blocks[0]
    occ = block["duplicates"][0]["occurrences"]
    assert {o["source_file"] for o in occ} == {"PROJECT_jan.xlsx", "PROJECT_feb.xlsx"}
    assert block["spans_files"] == 2


def test_nullable_violation_caught(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "nullable"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    kinds = {v["kind"] for v in project["violations"]}
    assert "nullable_violation" in kinds


def test_clean_run_xlsx_has_summary_and_per_table_sheets(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "clean"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    wb = load_workbook(out / "quality_report.xlsx", read_only=True)
    assert "Summary" in wb.sheetnames
    assert "PROJECT" in wb.sheetnames
    # No rejected sheet on a clean run.
    assert "PROJECT_rejected" not in wb.sheetnames
    wb.close()


def test_dup_pk_xlsx_has_rejected_sheet_with_pk_named_columns(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "dup_pk"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    wb = load_workbook(out / "quality_report.xlsx", read_only=True)
    assert "PROJECT_rejected" in wb.sheetnames
    ws = wb["PROJECT_rejected"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    # PK columns should appear by their actual contract name.
    assert "proj_id" in headers
    assert "column1" in headers
    assert "Source File" in headers
    assert "Source Row" in headers
    wb.close()


def test_no_tables_block_discovers_all_contracts(repo_root: Path, tmp_path: Path, monkeypatch):
    """The shipped epic 1118 validation.yaml has no `tables:` block — every
    contract under epics/1118/contracts/ should be picked up automatically.

    `samples/clean` has per-table xlsx files (PROJECT.xlsx, CALENDAR.xlsx)
    matched by the shipped `{table}*.xlsx` default pattern.
    """
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--input-dir", str(repo_root / "epics" / "1118"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    payload = __import__("json").loads((out / "quality_report.json").read_text(encoding="utf-8"))
    tables_validated = {t["table"] for t in payload["tables"]}
    assert tables_validated == {"PROJECT", "CALENDAR"}


def test_table_placeholder_in_file_pattern_resolves_per_table(repo_root: Path, tmp_path: Path, monkeypatch):
    """`file_pattern: "{table}*.xlsx"` becomes "PROJECT*.xlsx" / "CALENDAR*.xlsx"
    at glob time. Each table sees only its own files even when the input dir
    contains files for multiple tables."""
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--input-dir", str(repo_root / "epics" / "1118"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    payload = __import__("json").loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    calendar = next(t for t in payload["tables"] if t["table"] == "CALENDAR")
    # Each table sees only the file matching its name placeholder.
    assert {f["path"] for f in project["input"]["files"]} == {"PROJECT.xlsx"}
    assert {f["path"] for f in calendar["input"]["files"]} == {"CALENDAR.xlsx"}


def test_no_flags_uses_file_pattern_to_locate_data(repo_root: Path, monkeypatch):
    """Zero flags: `file_pattern` in validation.yaml (currently `sample/{table}*.xlsx`)
    resolves under epics/1118/ and finds the data automatically."""
    monkeypatch.chdir(repo_root)
    rc = main(["validate-data", "--epic", "1118"])
    assert rc == 0
    expected = repo_root / "epics" / "1118" / "validations" / "quality_report.json"
    assert expected.is_file()


def test_input_dir_override_against_external_fixture(repo_root: Path, tmp_path: Path, monkeypatch):
    """--input-dir overrides the epic_dir base. With the shipped file_pattern
    `sample/{table}*.xlsx`, pointing --input-dir at a dir that has a sample/
    subdir lets you validate an arbitrary external dataset.
    """
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    # Build an external dir with a sample/ subdir holding the dirty fixtures.
    ext = tmp_path / "external"
    (ext / "sample").mkdir(parents=True)
    import shutil
    for f in (repo_root / "epics" / "1118" / "sample_dirty").iterdir():
        shutil.copy2(f, ext / "sample" / f.name)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--input-dir", str(ext),  # absolute path
        "--output-dir", str(out),
    ])
    assert rc == 2  # dirty fixtures have seeded violations


def test_default_output_dir_lands_under_epic_validations(repo_root: Path, monkeypatch):
    """Omit --output-dir entirely; reports land in epics/<epic>/validations/."""
    monkeypatch.chdir(repo_root)
    rc = main(["validate-data", "--epic", "1118"])
    assert rc == 0
    expected = repo_root / "epics" / "1118" / "validations" / "quality_report.json"
    assert expected.is_file()


def test_no_input_files_violation_includes_diagnostic(repo_root: Path, tmp_path: Path, monkeypatch):
    """The no_input_files violation should include the absolute search base,
    the file_pattern, the resolved pattern, and a sample listing of what's
    actually in the search dir."""
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    # Point file_pattern at a subdir that exists but has no matching files (typo case).
    fake_epic = tmp_path / "epics" / "1118"
    (fake_epic / "configs" / "parsers").mkdir(parents=True)
    (fake_epic / "configs" / "validation.yaml").write_text("""
defaults:
  format: excel
  file_pattern: "sample/PROJEKT*.xlsx"
""", encoding="utf-8")
    # Copy contracts so the runner has something to validate against.
    contracts_src = repo_root / "epics" / "1118" / "contracts"
    contracts_dst = fake_epic / "contracts"
    contracts_dst.mkdir(parents=True)
    for f in contracts_src.glob("*.yaml"):
        import shutil
        shutil.copy2(f, contracts_dst / f.name)
    # Put one decoy file in the searched subdir.
    (fake_epic / "sample").mkdir()
    (fake_epic / "sample" / "PROJECT.xlsx").write_text("decoy")

    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(tmp_path / "epics"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project_table = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    v = next(v for v in project_table["violations"] if v["kind"] == "no_input_files")

    # `expected` includes the resolved pattern + the absolute base.
    assert "sample/PROJEKT*.xlsx" in v["expected"]
    assert str(fake_epic.resolve()) in v["expected"]

    # The diagnostic payload carries listings of what's actually there.
    diag = v["sample_offending_values"][0]["value"]
    assert diag["file_pattern"] == "sample/PROJEKT*.xlsx"
    assert diag["resolved_pattern"] == "sample/PROJEKT*.xlsx"
    assert "sample/" in diag["existing_top_level"]
    # The "did you mean PROJECT.xlsx?" hint shows up in the subdir listing.
    assert diag["subdir_searched"] == "sample"
    assert "PROJECT.xlsx" in diag["existing_in_subdir"]


def test_no_input_files_diagnostic_when_base_missing(repo_root: Path, tmp_path: Path, monkeypatch):
    """When the search base itself doesn't exist, the diagnostic flags it."""
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    fake_epic = tmp_path / "epics" / "1118"
    (fake_epic / "configs" / "parsers").mkdir(parents=True)
    (fake_epic / "configs" / "validation.yaml").write_text("""
defaults:
  format: excel
  file_pattern: "nope/{table}*.xlsx"
""", encoding="utf-8")
    contracts_dst = fake_epic / "contracts"
    contracts_dst.mkdir(parents=True)
    for f in (repo_root / "epics" / "1118" / "contracts").glob("*.yaml"):
        import shutil
        shutil.copy2(f, contracts_dst / f.name)

    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(tmp_path / "epics"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    v = next(
        v for t in payload["tables"]
        for v in t["violations"] if v["kind"] == "no_input_files"
    )
    diag = v["sample_offending_values"][0]["value"]
    # The base does exist (fake_epic), but the diagnostic still listed it.
    assert "existing_top_level" in diag


def test_missing_input_dir_returns_1(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--input-dir", str(tmp_path / "nonexistent"),
        "--output-dir", str(out),
    ])
    assert rc == 1
