"""Wraps the four report formats so the runner has a single call."""

from __future__ import annotations

from pathlib import Path

from data_contract.contract import Contract
from data_contract.validate_data.report.html import write_html
from data_contract.validate_data.report.json_report import write_json
from data_contract.validate_data.report.markdown import write_markdown
from data_contract.validate_data.report.xlsx import write_xlsx
from data_contract.validate_data.runner import ValidationReport


def write_all(
    report: ValidationReport,
    out_dir: Path,
    contracts_by_table: dict[str, Contract],
) -> dict[str, Path]:
    """Emit HTML + XLSX + JSON + Markdown into `out_dir`.

    HTML + XLSX are the business-facing pair (HTML for browser/dashboards,
    XLSX for offline / spreadsheet users). JSON + Markdown are the engineer-
    facing pair (JSON for machines/dashboards, Markdown for PR comments / CI).
    """
    html_path  = write_html(report, contracts_by_table, out_dir / "quality_report.html")
    xlsx_path  = write_xlsx(report, contracts_by_table, out_dir / "quality_report.xlsx")
    json_path  = write_json(report, contracts_by_table, out_dir / "quality_report.json")
    md_path    = write_markdown(report, out_dir / "quality_report.md")
    return {"html": html_path, "xlsx": xlsx_path, "json": json_path, "markdown": md_path}
