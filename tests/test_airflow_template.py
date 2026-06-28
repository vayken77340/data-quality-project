"""Lint check on the medallion DAG template.

The DAG isn't executed in CI -- Airflow is not a test dependency.
This test only confirms the file parses cleanly (no syntax errors
introduced by drift in the template).
"""

from __future__ import annotations

import py_compile
from pathlib import Path


def test_medallion_dag_template_compiles():
    template = Path("airflow_templates") / "medallion_dag.py"
    assert template.is_file(), f"DAG template missing: {template}"
    py_compile.compile(str(template), doraise=True)
