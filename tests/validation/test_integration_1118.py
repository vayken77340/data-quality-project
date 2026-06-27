"""End-to-end integration tests for `validate-data` on epic 1118."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from openpyxl import load_workbook

from data_contract.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "1118"


def _outdir(tmp_path: Path) -> Path:
    return tmp_path / "validations"


def _postgres_epic_root(repo_root: Path, tmp_path: Path) -> Path:
    """Copy the live `epics/1118/` config tree to tmp, rewrite every
    generated contract's `target:` to postgres, and normalise the table
    names to the historical `PROJECT` / `CALENDAR` shape these tests use.

    The live 1118 spec maps tables to `ipn_project` / `ipn_calendar`
    (snake_case database identifiers); these integration tests pre-date
    that rename and reference the plain sheet names. Rewriting the
    in-tmp copies keeps the tests focused on behaviour rather than
    spec-naming churn.

    Target: the live config uses `target: oracle` (Y/N boolean tokens).
    The fixture data uses Python booleans surfaced as 'True'/'False'
    -- postgres accepts those, oracle does not. Tests are about
    epic-resolution / PK clustering / etc., not target-specific behaviour.
    """
    dst_root = tmp_path / "tmp_epics"
    dst = dst_root / "1118"
    src = repo_root / "epics" / "1118"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    # Rename the canonical and history contract YAMLs to the legacy table
    # names so `--table PROJECT` and the fixture sample filenames line up.
    rename_map = {"ipn_project": "PROJECT", "ipn_calendar": "CALENDAR"}
    contracts_dir = dst / "contracts"
    for old_name, new_name in rename_map.items():
        for legacy_path in (
            contracts_dir / f"{old_name}.yaml",
            *contracts_dir.glob(f"history/*/{old_name}.yaml"),
        ):
            if legacy_path.is_file():
                legacy_path.rename(legacy_path.with_name(f"{new_name}.yaml"))

    # Rewrite each contract YAML in place: `table:` -> legacy name,
    # `target:` -> postgres.
    contract_paths = list(contracts_dir.glob("*.yaml"))
    history_dir = contracts_dir / "history"
    if history_dir.is_dir():
        contract_paths.extend(history_dir.rglob("*.yaml"))
    for cy in contract_paths:
        text = cy.read_text(encoding="utf-8")
        text = re.sub(r"^target:\s*\w+", "target: postgres", text, count=1, flags=re.MULTILINE)
        for old_name, new_name in rename_map.items():
            text = re.sub(rf"^table:\s*{old_name}\s*$", f"table: {new_name}", text, count=1, flags=re.MULTILINE)
        cy.write_text(text, encoding="utf-8")
    return dst_root


def test_clean_run_passes_and_emits_four_reports(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "clean"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    # Four formats now: HTML + XLSX (business) + JSON + Markdown (engineer).
    assert (out / "quality_report.html").exists()
    assert (out / "quality_report.xlsx").exists()
    assert (out / "quality_report.json").exists()
    assert (out / "quality_report.md").exists()

    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["pass"] is True
    assert payload["summary"]["by_severity"]["error"] == 0
    assert payload["tables"][0]["table"] == "PROJECT"


def test_dup_pk_fails_with_clustered_violations(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
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
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
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
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "nullable"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    kinds = {v["kind"] for v in project["violations"]}
    assert "nullable_violation" in kinds


def test_clean_run_xlsx_has_summary_and_profile_sheets(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "clean"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    wb = load_workbook(out / "quality_report.xlsx", read_only=True)
    # Gold-standard layout: Run / Summary / Profile / Checks.
    assert "Run" in wb.sheetnames
    assert "Summary" in wb.sheetnames
    assert "Profile" in wb.sheetnames        # unified across tables
    assert "Checks" in wb.sheetnames
    # Standalone Dimensions sheet was merged into Summary.
    assert "Dimensions" not in wb.sheetnames
    # No per-table profile sheets; everything is on the global Profile sheet.
    assert "PROJECT_profile" not in wb.sheetnames
    # No rejected sheet on a clean run.
    assert "PROJECT_rejected" not in wb.sheetnames
    assert "PROJECT_rejected_rows" not in wb.sheetnames
    assert "PROJECT_rejected_violations" not in wb.sheetnames
    wb.close()


def test_dup_pk_xlsx_has_one_rejected_sheet_with_pk_after_source_file(repo_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--table", "PROJECT",
        "--input-dir", str(FIXTURES / "dup_pk"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    wb = load_workbook(out / "quality_report.xlsx", read_only=True)
    # Single consolidated rejected sheet per table.
    assert "PROJECT_rejected" in wb.sheetnames
    assert "PROJECT_rejected_rows" not in wb.sheetnames
    assert "PROJECT_rejected_violations" not in wb.sheetnames

    rj = wb["PROJECT_rejected"]
    headers = [rj.cell(row=1, column=c).value for c in range(1, rj.max_column + 1)]
    # One row per violation. Layout:
    #   Severity | Source File | proj_id | column1 | <violating fields only> | Check | Expected.
    assert headers[0] == "Severity"           # severity sits first
    assert headers[1] == "Source File"
    assert headers[2] == "proj_id"            # composite PK after Source File
    assert headers[3] == "column1"
    assert "Worst Severity" not in headers
    assert "Source Row" not in headers
    # Right-hand annotation columns:
    assert headers[-2] == "Check"
    assert headers[-1] == "Expected"
    # Contract-field block only shows fields that actually have a violation.
    # The dup_pk fixture triggers boolean-token (column1) and type-coercion
    # (column2) violations on top of pk_not_unique. column3 / column4 are
    # clean string values; they must NOT appear as field columns.
    for non_violating in ("column3", "column4"):
        assert headers[4:-2].count(non_violating) == 0, (
            f"non-violating field {non_violating!r} should not have a column"
        )
    # Severity values uppercased.
    for r in range(2, rj.max_row + 1):
        sev = rj.cell(row=r, column=1).value
        assert sev == sev.upper(), f"severity not uppercased at row {r}: {sev!r}"
    wb.close()


def test_no_tables_block_discovers_all_contracts(repo_root: Path, tmp_path: Path, monkeypatch):
    """The shipped epic 1118 validation.yaml has no `tables:` block — every
    contract under epics/1118/contracts/ should be picked up automatically."""
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--input-dir", str(epic_root / "1118"),
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
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(epic_root),
        "--input-dir", str(epic_root / "1118"),
        "--output-dir", str(out),
    ])
    assert rc == 0
    payload = __import__("json").loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    calendar = next(t for t in payload["tables"] if t["table"] == "CALENDAR")
    assert {f["path"] for f in project["input"]["files"]} == {"PROJECT.xlsx"}
    assert {f["path"] for f in calendar["input"]["files"]} == {"CALENDAR.xlsx"}


def test_no_flags_uses_file_pattern_to_locate_data(repo_root: Path, tmp_path: Path, monkeypatch):
    """Zero flags: `file_pattern` in validation.yaml (currently `sample/{table}*.xlsx`)
    resolves under epics/1118/ and finds the data automatically."""
    monkeypatch.chdir(repo_root)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main(["validate-data", "--epic", "1118", "--epic-root", str(epic_root)])
    assert rc == 0
    expected = epic_root / "1118" / "validations" / "quality_report.json"
    assert expected.is_file()


def test_input_dir_override_against_external_fixture(repo_root: Path, tmp_path: Path, monkeypatch):
    """--input-dir overrides the epic_dir base. With the shipped file_pattern
    `sample/{table}*.xlsx`, pointing --input-dir at a dir that has a sample/
    subdir lets you validate an arbitrary external dataset.

    We seed the external dataset by copying the clean fixtures from epic
    1118 and then blanking the first data row's PK in PROJECT -- guarantees
    at least one error (`nullable_violation` on a non-nullable PK column),
    so the CLI returns exit code 2.
    """
    import shutil
    from openpyxl import load_workbook

    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    ext = tmp_path / "external"
    (ext / "sample").mkdir(parents=True)
    # Copy the clean fixtures into the external sample/ directory.
    for f in (repo_root / "epics" / "1118" / "sample").iterdir():
        shutil.copy2(f, ext / "sample" / f.name)
    # Dirty PROJECT.xlsx: blank the first data row's `proj_id` (the
    # non-nullable PK -- a guaranteed violation). The PROJECT contract
    # declares proj_id as the first field so it lands in column A.
    project_xlsx = ext / "sample" / "PROJECT.xlsx"
    wb = load_workbook(project_xlsx)
    ws = wb.active
    # Row 1 is the header per the default `header_row: 1` parser config;
    # row 2 is the first data row. Blank A2 to trigger the violation.
    ws["A2"] = None
    wb.save(project_xlsx)

    rc = main([
        "validate-data",
        "--epic", "1118",
        "--input-dir", str(ext),  # absolute path
        "--output-dir", str(out),
    ])
    assert rc == 2  # the seeded null PK trips a nullable_violation


def test_default_output_dir_lands_under_epic_validations(repo_root: Path, tmp_path: Path, monkeypatch):
    """Omit --output-dir entirely; reports land in <epic_root>/<epic>/validations/."""
    monkeypatch.chdir(repo_root)
    epic_root = _postgres_epic_root(repo_root, tmp_path)
    rc = main(["validate-data", "--epic", "1118", "--epic-root", str(epic_root)])
    assert rc == 0
    expected = epic_root / "1118" / "validations" / "quality_report.json"
    assert expected.is_file()


def test_no_input_files_violation_includes_diagnostic(repo_root: Path, tmp_path: Path, monkeypatch):
    """The no_input_files violation should include the absolute search base,
    the file_pattern, the resolved pattern, and a sample listing of what's
    actually in the search dir."""
    monkeypatch.chdir(repo_root)
    out = _outdir(tmp_path)
    # Point file_pattern at a subdir that exists but has no matching files (typo case).
    fake_epic = tmp_path / "epics" / "1118"
    from tests.conftest import ALL_CHECKS_ENABLED_YAML
    (fake_epic / "configs").mkdir(parents=True, exist_ok=True)
    (fake_epic / "configs" / "validation.yaml").write_text(
        ALL_CHECKS_ENABLED_YAML + """
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
    # Rewrite copied contracts' target to postgres so the runner doesn't try
    # to load the oracle overlay (the test only cares about file-glob diagnostics).
    # Also rename the live `ipn_project`/`ipn_calendar` tables back to the
    # legacy names this test asserts on.
    rename_map = {"ipn_project": "PROJECT", "ipn_calendar": "CALENDAR"}
    for old_name, new_name in rename_map.items():
        legacy = contracts_dst / f"{old_name}.yaml"
        if legacy.is_file():
            legacy.rename(legacy.with_name(f"{new_name}.yaml"))
    for f in contracts_dst.glob("*.yaml"):
        t = f.read_text(encoding="utf-8")
        t = re.sub(r"^target:\s*\w+", "target: postgres", t, count=1, flags=re.MULTILINE)
        for old_name, new_name in rename_map.items():
            t = re.sub(rf"^table:\s*{old_name}\s*$", f"table: {new_name}", t, count=1, flags=re.MULTILINE)
        f.write_text(t, encoding="utf-8")
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
    # `no_input_files` is a table-level violation: it lives in `run_issues`,
    # not in `tables[].violations` (which holds row-level aggregates).
    v = next(
        i for i in payload["run_issues"]
        if i["kind"] == "no_input_files" and i["table"] == "PROJECT"
    )

    # `expected` includes the resolved pattern + the absolute base.
    assert "sample/PROJEKT*.xlsx" in v["expected"]
    assert str(fake_epic.resolve()) in v["expected"]

    # The diagnostic payload carries listings of what's actually there.
    diag = v["offending_value"]
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
    from tests.conftest import ALL_CHECKS_ENABLED_YAML
    (fake_epic / "configs").mkdir(parents=True, exist_ok=True)
    (fake_epic / "configs" / "validation.yaml").write_text(
        ALL_CHECKS_ENABLED_YAML + """
defaults:
  format: excel
  file_pattern: "nope/{table}*.xlsx"
""", encoding="utf-8")
    contracts_dst = fake_epic / "contracts"
    contracts_dst.mkdir(parents=True)
    for f in (repo_root / "epics" / "1118" / "contracts").glob("*.yaml"):
        import shutil
        shutil.copy2(f, contracts_dst / f.name)
    for f in contracts_dst.glob("*.yaml"):
        t = f.read_text(encoding="utf-8")
        t = re.sub(r"^target:\s*\w+", "target: postgres", t, count=1, flags=re.MULTILINE)
        f.write_text(t, encoding="utf-8")

    rc = main([
        "validate-data",
        "--epic", "1118",
        "--epic-root", str(tmp_path / "epics"),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    v = next(i for i in payload["run_issues"] if i["kind"] == "no_input_files")
    diag = v["offending_value"]
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


# ---------------------------------------------------------------------------
# French boolean tokens (VRAI/FAUX/OUI/NON) through the full pipeline
# ---------------------------------------------------------------------------


def _build_french_boolean_epic(
    repo_root: Path,
    epic_root_parent: Path,
    column1_values: list[object],
) -> Path:
    """Create a tmp epics/1118/ tree with the real PROJECT/CALENDAR contracts
    and a fresh sample/PROJECT.xlsx whose `column1` boolean is filled with
    arbitrary tokens — typically French (VRAI/FAUX/OUI/NON), or a mix that
    seeds a coercion violation. Returns the epic-root path to pass to --epic-root.
    """
    import shutil
    from openpyxl import Workbook

    epic_root = epic_root_parent / "epics"
    fake_epic = epic_root / "1118"
    from tests.conftest import ALL_CHECKS_ENABLED_YAML
    (fake_epic / "configs").mkdir(parents=True, exist_ok=True)
    (fake_epic / "configs" / "validation.yaml").write_text(
        ALL_CHECKS_ENABLED_YAML +
        "defaults:\n"
        "  format: excel\n"
        '  file_pattern: "sample/{table}*.xlsx"\n',
        encoding="utf-8",
    )
    contracts_src = repo_root / "epics" / "1118" / "contracts"
    contracts_dst = fake_epic / "contracts"
    contracts_dst.mkdir(parents=True)
    for f in contracts_src.glob("*.yaml"):
        shutil.copy2(f, contracts_dst / f.name)
    # Rename copied `ipn_project`/`ipn_calendar` contracts back to PROJECT/
    # CALENDAR (legacy table names this test asserts on).
    rename_map = {"ipn_project": "PROJECT", "ipn_calendar": "CALENDAR"}
    for old_name, new_name in rename_map.items():
        legacy = contracts_dst / f"{old_name}.yaml"
        if legacy.is_file():
            legacy.rename(legacy.with_name(f"{new_name}.yaml"))
    # Rewrite contract targets to postgres (this test asserts on postgres
    # boolean tokens) and normalise the table name to the legacy form.
    for f in contracts_dst.glob("*.yaml"):
        t = f.read_text(encoding="utf-8")
        t = re.sub(r"^target:\s*\w+", "target: postgres", t, count=1, flags=re.MULTILINE)
        for old_name, new_name in rename_map.items():
            t = re.sub(rf"^table:\s*{old_name}\s*$", f"table: {new_name}", t, count=1, flags=re.MULTILINE)
        f.write_text(t, encoding="utf-8")

    (fake_epic / "sample").mkdir()
    wb = Workbook()
    ws = wb.active
    ws.title = "PROJECT"
    ws.append(["proj_id", "column1", "column2", "column3", "column4"])
    for i, raw in enumerate(column1_values, start=1):
        ws.append([i, raw, "2024-01-15T10:00:00", f"a{i}", f"b{i}"])
    wb.save(fake_epic / "sample" / "PROJECT.xlsx")
    return epic_root


def test_boolean_tokens_from_target_accepted_end_to_end(repo_root: Path, tmp_path: Path, monkeypatch):
    """Boolean tokens declared by the active target (postgres native: t/f/yes/no/y/n
    /on/off/true/false/1/0) flow through the pipeline cleanly. Tokens are NOT
    inherited from configs/types.yaml -- the target is authoritative."""
    monkeypatch.chdir(repo_root)
    epic_root = _build_french_boolean_epic(
        repo_root, tmp_path, ["true", "false", "Yes", " no "],
    )
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--epic-root", str(epic_root),
        "--output-dir", str(out),
    ])
    assert rc == 0, f"expected clean run, got rc={rc}"
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    kinds = {v["kind"] for v in project["violations"]}
    assert "boolean_coercion_violation" not in kinds
    assert "type_coercion_violation" not in kinds
    assert "nullable_violation" not in kinds


def test_unknown_boolean_token_flagged_as_coercion_violation(
    repo_root: Path, tmp_path: Path, monkeypatch
):
    """A token outside the active target's declared `data_values` list (e.g.
    'maybe') surfaces as a boolean_coercion_violation."""
    monkeypatch.chdir(repo_root)
    epic_root = _build_french_boolean_epic(
        repo_root, tmp_path, ["true", "maybe", "false"],
    )
    out = _outdir(tmp_path)
    rc = main([
        "validate-data",
        "--epic", "1118",
        "--table", "PROJECT",
        "--epic-root", str(epic_root),
        "--output-dir", str(out),
    ])
    assert rc == 2
    payload = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    project = next(t for t in payload["tables"] if t["table"] == "PROJECT")
    bool_violations = [
        v for v in project["violations"]
        if v["kind"] == "boolean_coercion_violation" and v["field"] == "column1"
    ]
    assert len(bool_violations) == 1
    block = bool_violations[0]
    # Expected message lists the active target's (postgres) tokens.
    expected_msg = block["expected"]
    for token in ("true", "false", "yes", "no", "t", "f"):
        assert token in expected_msg, f"missing token {token!r} in expected={expected_msg!r}"
