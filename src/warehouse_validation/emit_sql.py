"""Report emission for the warehouse runner.

Builds a `ValidationReport` whose single `TableReport` carries the SQL
COUNT-based violations and hands it to the dq_core 4-format writer.
Rejected-rows, metrics, source_schemas, profile and score stay empty --
the warehouse path is constraint pushdown only, no row pulls.
"""

from __future__ import annotations

from pathlib import Path

from dq_core._util import now_iso_z
from dq_core.report.writer import write_all
from dq_core.report_build import build_run_metadata
from dq_core.report_models import TableReport, ValidationReport
from dq_core.settings import load_settings

from warehouse_validation.setup import RunSetup


def write(
    *,
    setup: RunSetup,
    table_report: TableReport,
    duration_ms: int,
    cli_args: list[str],
    subdir: str | None = None,
) -> dict[str, Path]:
    """Build the ValidationReport, dump all four formats, return the
    paths keyed by format name.

    When `subdir` is set, reports land under `output_dir / subdir`
    instead of `output_dir`. Phase 3's bronze and reconcile runners
    pass `"bronze"` / `"reconcile"` so the three runners' output
    sets stay distinct under one `--output-dir`. Silver passes None
    and keeps writing to `output_dir` directly.
    """
    generated_at = now_iso_z()
    settings = load_settings()

    run_meta = build_run_metadata(
        epic=setup.epic,
        generated_at=generated_at,
        duration_ms=duration_ms,
        config=setup.config,
        target_config=None,
        contracts_by_table={setup.config.table_name: setup.contract},
        types_path=Path(""),
        table_reports=[table_report],
    )
    # build_run_metadata reads sys.argv directly; override here so the
    # report reflects the actual invocation rather than pytest's argv when
    # called from a test.
    run_meta.cli_args = list(cli_args)

    report = ValidationReport(
        epic=setup.epic,
        generated_at=generated_at,
        table_reports=[table_report],
        settings=settings,
        run_metadata=run_meta,
    )

    out_dir = (
        setup.config.output_dir if subdir is None
        else setup.config.output_dir / subdir
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_all(
        report=report,
        out_dir=out_dir,
        contracts_by_table={setup.config.table_name: setup.contract},
    )
