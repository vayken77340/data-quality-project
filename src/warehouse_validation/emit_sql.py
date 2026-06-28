"""Report emission for the warehouse runners.

Builds a `ValidationReport` whose `TableReport`s carry SQL-driven
violations and hands it to the dq_core 4-format writer.
Rejected-rows, metrics, source_schemas, profile and score stay empty
on the warehouse path -- pushdown only, no row pulls.

Silver / bronze / reconcile callers each pass a single TableReport
wrapped in a one-element list plus a one-key contracts_by_table.
The gold runner passes a multi-table list and contracts_by_table dict
keyed by every distinct rule.table that has a sidecar contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from dq_core._util import now_iso_z
from dq_core.contract import Contract
from dq_core.gates import Gates
from dq_core.report.writer import write_all
from dq_core.report_build import build_run_metadata
from dq_core.report_models import TableReport, ValidationReport
from dq_core.settings import load_settings

from warehouse_validation import __version__ as _tool_version


class _RunMetadataConfig(Protocol):
    """The slice of a validation config that build_run_metadata reads.
    Satisfied by WarehouseValidationConfig (silver/bronze/reconcile)
    and the gold runner's minimal `_GoldConfig`."""
    checks: Gates


def write(
    *,
    epic: str,
    output_dir: Path,
    config: _RunMetadataConfig,
    table_reports: list[TableReport],
    contracts_by_table: dict[str, Contract],
    duration_ms: int,
    cli_args: list[str],
    subdir: str | None = None,
) -> dict[str, Path]:
    """Build the ValidationReport, dump all four formats, return the
    paths keyed by format name.

    When `subdir` is set, reports land under `output_dir / subdir`
    instead of `output_dir`. Bronze passes `"bronze"`, reconcile
    `"reconcile"`, gold `"gold"`; silver passes None and writes to
    `output_dir` directly.
    """
    generated_at = now_iso_z()
    settings = load_settings()

    run_meta = build_run_metadata(
        epic=epic,
        generated_at=generated_at,
        duration_ms=duration_ms,
        config=config,
        target_config=None,
        contracts_by_table=contracts_by_table,
        types_path=Path(""),
        table_reports=table_reports,
        tool_version=_tool_version,
    )
    # build_run_metadata reads sys.argv directly; override here so the
    # report reflects the actual invocation rather than pytest's argv when
    # called from a test.
    run_meta.cli_args = list(cli_args)

    report = ValidationReport(
        epic=epic,
        generated_at=generated_at,
        table_reports=table_reports,
        settings=settings,
        run_metadata=run_meta,
    )

    out_dir = output_dir if subdir is None else output_dir / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_all(
        report=report,
        out_dir=out_dir,
        contracts_by_table=contracts_by_table,
    )
