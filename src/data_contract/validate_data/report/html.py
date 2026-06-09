"""HTML report -- business-facing self-contained file.

Single-file output with inline CSS and no JS. Uses Jinja2 to render the same
payload model the JSON writer produces, so the two formats stay in lockstep.

Sections (in order):
  1. Header banner (epic, status, score) -- color-coded
  2. Executive summary card (counts, target, generated_at)
  3. Quality dimensions panel (4-card grid)
  4. Per-table table (traffic-light rows)
  5. Top issues (plain-English, with action hints)
  6. Run issues (operational failures, only if any)
  7. Per-table data profile (collapsible <details>)
  8. Per-table rejected rows (collapsible <details>, full source-row context)
  9. Run metadata footer
"""

from __future__ import annotations

from pathlib import Path

from data_contract.contract import Contract
from data_contract.validate_data.report.json_report import render_json
from data_contract.validate_data.runner import ValidationReport


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAME = "quality_report.html.j2"


def render_html(report: ValidationReport, contracts_by_table: dict[str, Contract]) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(_TEMPLATE_NAME)
    payload = render_json(report, contracts_by_table)
    return template.render(p=payload)


def write_html(
    report: ValidationReport,
    contracts_by_table: dict[str, Contract],
    out_path: Path,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(report, contracts_by_table), encoding="utf-8")
    return out_path
