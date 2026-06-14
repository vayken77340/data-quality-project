"""End-to-end target-aware validation.

Constructs a tmp epic with two contracts (numeric + string + boolean) and a
CSV of test rows. Validates the same data against postgres, oracle, and
iceberg targets and asserts the violation profiles differ where the targets'
rules diverge.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import yaml

from data_contract.cli import main


def _write_epic(
    tmp_path: Path,
    repo_root: Path,
    *,
    target: str | None,
) -> tuple[Path, Path]:
    """Build a tmp epic with:
      - contracts/T.yaml: id(int32), label(string max_length=3), flag(boolean)
      - sample/T.csv: rows that trip target-specific rules

    Returns (epic_dir, sample_dir).
    """
    epic_dir = tmp_path / "epics" / "TEST"
    (epic_dir / "configs").mkdir(parents=True)
    (epic_dir / "contracts").mkdir()
    (epic_dir / "sample").mkdir()

    # Mirror the repo-root configs (types.yaml + targets/) under tmp so the
    # runner resolves them correctly.
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "types.yaml").write_text(
        (repo_root / "configs" / "types.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "configs" / "parsers.yaml").write_text(
        (repo_root / "configs" / "parsers.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "configs" / "targets").mkdir()
    for name in ("postgres.yaml", "oracle.yaml", "iceberg.yaml"):
        (tmp_path / "configs" / "targets" / name).write_text(
            (repo_root / "configs" / "targets" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    contract = {
        "version": "1.0",
        "epic": "TEST",
        "generated_at": "2026-06-09T00:00:00Z",
        "source": {"spec_file": "spec.xlsx", "spec_sheet": "T"},
        "table": "T",
        "fields": [
            {"name": "id", "type": "int32", "nullable": False, "primary_key": True},
            {"name": "label", "type": "string", "nullable": False, "max_length": 3},
            {"name": "flag", "type": "boolean", "nullable": False},
        ],
    }
    (epic_dir / "contracts" / "T.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8",
    )

    # Required tier-keyed checks + metrics blocks. Descriptions live in
    # configs/report_strings.yaml under `checks_sheet.descriptions`.
    checks_block = {
        "structural": {
            "type_coercion": True, "boolean_coercion": True,
            "nullable": True, "max_length": True,
            "field_names_from_sample": False, "field_types_from_sample": False,
        },
        "table": {
            "column_missing": True, "pk_uniqueness": True, "fk_existence": True,
        },
        "field": {
            "allowed_values": True, "pattern": True, "min_value": True,
            "max_value": True, "format": True, "unique": True,
        },
    }
    metrics_block = {
        "field": {
            "null_count": True, "null_percentage": True, "distinct_count": True,
            "completeness": True, "duplicate_pct": True,
        },
        "table": {"row_count": True},
    }
    validation_yaml = {
        "defaults": {"format": "csv", "file_pattern": "sample/{table}*.csv"},
        "checks": checks_block,
        "metrics": metrics_block,
    }
    # `target:` is required -- callers pass an explicit target name.
    validation_yaml["target"] = target
    (epic_dir / "configs" / "validation.yaml").write_text(
        yaml.safe_dump(validation_yaml, sort_keys=False), encoding="utf-8",
    )

    # The CSV is deliberately crafted around three discriminators:
    #
    # Row 1: id=42, label="abc", flag="true"
    #   * id ok everywhere
    #   * label 3 chars / 3 bytes -> pass everywhere
    #   * flag "true": postgres ACCEPT (native bool), oracle REJECT (Y/N only),
    #                  iceberg ACCEPT (true/false canonical)
    #
    # Row 2: id=42, label="abc", flag="Y"
    #   * flag "Y": postgres ACCEPT (`y` is in postgres `boolin` set),
    #               oracle ACCEPT (Y/N), iceberg REJECT (only true/false).
    #
    # Row 3: id=3000000000, label="abc", flag="true"
    #   * id overflows int32 intrinsic range -> rejected by ALL targets
    #     (even target=None, since intrinsic int32 bounds always apply).
    csv = dedent("""\
        id,label,flag
        42,abc,true
        43,abc,Y
        3000000000,abc,true
    """)
    (epic_dir / "sample" / "T.csv").write_text(csv, encoding="utf-8")

    return epic_dir, epic_dir / "sample"


def _run_validate_data(tmp_path: Path, repo_root: Path, target: str | None) -> Path:
    """Set up tmp epic, run validate-data CLI, return path to the JSON report."""
    epic_dir, _ = _write_epic(tmp_path, repo_root, target=target)
    out_dir = tmp_path / "out"
    rc = main([
        "validate-data",
        "--epic", "TEST",
        "--epic-root", str(tmp_path / "epics"),
        "--input-dir", str(epic_dir),
        "--output-dir", str(out_dir),
        "--types", str(tmp_path / "configs" / "types.yaml"),
    ])
    # rc is 2 when there are errors, 0 when clean; we only care about the
    # report contents for these assertions.
    assert rc in (0, 2)
    return out_dir / "quality_report.json"


def _violations_for_field(report_path: Path, field: str) -> list[dict]:
    import json
    data = json.loads(report_path.read_text(encoding="utf-8"))
    tbl = next(t for t in data["tables"] if t["table"] == "T")
    return [v for v in tbl["violations"] if v.get("field") == field]


# ---------------------------------------------------------------------------
# postgres: native bool tokens, int32 bounds
# ---------------------------------------------------------------------------


def _all_offending_values(violations: list[dict], kind: str) -> set[str]:
    """Flatten all `sample_offending_values[*].value` for violations of `kind`."""
    out: set[str] = set()
    for v in violations:
        if v.get("kind") != kind:
            continue
        for sample in v.get("sample_offending_values", []) or []:
            val = sample.get("value")
            if val is not None:
                out.add(str(val))
    return out


# ---------------------------------------------------------------------------
# int32 bounds: 3e9 is rejected by every target since they all declare 32-bit bounds
# ---------------------------------------------------------------------------


def test_int32_overflow_rejected_with_postgres(tmp_path: Path, repo_root: Path):
    report = _run_validate_data(tmp_path, repo_root, target="postgres")
    id_violations = _violations_for_field(report, "id")
    offenders = _all_offending_values(id_violations, "type_coercion_violation")
    assert "3000000000" in offenders


def test_int32_overflow_rejected_with_oracle(tmp_path: Path, repo_root: Path):
    """Oracle declares int32 bounds matching the 32-bit range (NUMBER(10, 0))."""
    report = _run_validate_data(tmp_path, repo_root, target="oracle")
    id_violations = _violations_for_field(report, "id")
    offenders = _all_offending_values(id_violations, "type_coercion_violation")
    assert "3000000000" in offenders


# ---------------------------------------------------------------------------
# Boolean token discrimination
# ---------------------------------------------------------------------------
#
# Boolean tokens are now contract-level, not target-level. Generated contracts
# carry the universal token list from configs/types.yaml -- including French
# (vrai/faux/oui/non), English (true/false/yes/no/y/n/t/f), and digit (0/1)
# forms. The same token list applies regardless of which target the contract
# is validated against.
#
# Spec authors who want to narrow the accepted tokens can edit the generated
# contract's `data_values:` block per boolean field.


def _all_targets_accept_universal_bool_tokens(target: str, tmp_path: Path, repo_root: Path):
    """For any target, the universal token list applies: `true`, `Y`, `vrai`,
    `1`, etc. all parse as booleans because the contract is authoritative."""
    report = _run_validate_data(tmp_path, repo_root, target=target)
    flag_violations = _violations_for_field(report, "flag")
    offenders = _all_offending_values(flag_violations, "boolean_coercion_violation")
    for token in ("true", "Y"):
        assert token not in offenders, (
            f"{token!r} should be accepted under target {target!r} "
            f"(contract-level tokens, not target-level)"
        )


def test_postgres_accepts_universal_bool_tokens(tmp_path: Path, repo_root: Path):
    _all_targets_accept_universal_bool_tokens("postgres", tmp_path, repo_root)


def test_oracle_accepts_universal_bool_tokens(tmp_path: Path, repo_root: Path):
    _all_targets_accept_universal_bool_tokens("oracle", tmp_path, repo_root)


def test_iceberg_accepts_universal_bool_tokens(tmp_path: Path, repo_root: Path):
    _all_targets_accept_universal_bool_tokens("iceberg", tmp_path, repo_root)
