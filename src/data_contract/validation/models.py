"""Pure data models for the validation pipeline.

The runner orchestrates; this module defines the values that flow between
its phases:

  * `Violation`   -- one failed observation (re-exported from violations.py
                     for ergonomics; the canonical home stays there).
  * `RejectedRow` -- row-centric view aggregating every Violation on a
                     single (source_file, source_row).
  * `TableReport` -- per-table accumulator: violations, profile, score,
                     metrics, per-check status, source schemas.
  * `RunMetadata` -- run-level context surfaced in every report format.
  * `ValidationReport` -- the final per-run artifact.

Nothing here knows how to render a report; rendering lives in
`validation/report/`. Nothing here computes anything; computation lives
in `runner.py` and `post.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data_contract.validation.config import ValidationSettings
from data_contract.violations import Violation


@dataclass
class RejectedRow:
    """Row-centric view of a violating source row.

    Carries every contract field's value on the offending row so spec
    authors don't have to grep the source file to see the surrounding
    columns. Built once per affected (source_file, source_row) at report
    time.
    """
    source_file: str
    source_row: int
    pk_values: dict[str, Any]
    source_row_data: dict[str, Any]                # every contract field on this row
    violations: list[dict[str, Any]]               # each violation affecting this row
    worst_severity: str                            # "error" | "warning" | "info"


@dataclass
class TableReport:
    table: str
    contract_version: str
    pk_fields: list[str]
    input_files: list[tuple[Path, int]]
    violations: list[Violation] = field(default_factory=list)
    total_rows: int = 0
    # Extensions for the gold-standard report (populated after all checks run).
    profile: Any = None                            # report.profile.TableProfile | None
    score: Any = None                              # report.dimensions.TableScore | None
    rejected_rows: list[RejectedRow] = field(default_factory=list)
    rejected_rows_truncated: int = 0               # count beyond rejected_row_cap
    # Per-(table, check) status: dict[check_name, check_status.CheckStatus].
    by_check: dict[str, Any] = field(default_factory=dict)
    # Configurable metrics: dict[metric_name, metrics.MetricResult].
    metrics: dict[str, Any] = field(default_factory=dict)
    # Per-file parser-discovered schema, populated when the parser exposed
    # `ParserSchema` (CSV header, Excel header, JSON `report_header`, etc.).
    # Shape: list[tuple[filename, ParserSchema | None]].
    source_schemas: list[tuple[str, Any]] = field(default_factory=list)
    # Parser class used to load this table (stashed so the drift checks can
    # call its `normalize_source_type` for source-type translation).
    parser_cls: Any = None


@dataclass
class RunMetadata:
    """Run-level context surfaced in every report format.

    Closes the "two reports look identical but mean different things" gap:
    the target, gated checks, contract versions, and tool version that
    produced the run all flow into the `run` block.
    """
    epic: str
    generated_at: str
    duration_ms: int
    status: str                                    # "PASS" | "FAIL"
    status_reason: str
    tool_version: str
    target: dict[str, str] | None                  # {"name", "description"} | None
    checks_enabled: list[str]
    checks_disabled: list[str]
    checks_descriptions: dict[str, str]            # YAML-supplied, per check name
    contracts: dict[str, str]                      # table -> contract version
    types_yaml_path: str
    cli_args: list[str]


@dataclass
class ValidationReport:
    epic: str
    generated_at: str
    table_reports: list[TableReport]
    settings: ValidationSettings
    run_metadata: RunMetadata | None = None        # built by run_validate_data

    @property
    def has_errors(self) -> bool:
        return any(v.severity == "error" for tr in self.table_reports for v in tr.violations)

    @property
    def summary_counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for tr in self.table_reports:
            for v in tr.violations:
                if v.severity in out:
                    out[v.severity] += 1
        return out


__all__ = [
    "RejectedRow",
    "TableReport",
    "RunMetadata",
    "ValidationReport",
    "Violation",
]
