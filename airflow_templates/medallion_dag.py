"""Medallion validation DAG -- one example pipeline for epic 1118.

Tasks invoke the warehouse_validation CLI directly. Exit codes are
the contract:
    0 = pass, 1 = config error, 2 = data violations
Airflow treats nonzero as failure and short-circuits downstream tasks
via default trigger rules.

Operators: fork this file, adjust EPIC / TABLE / CONNECTOR /
EPIC_ROOT, and drop into your DAGs dir. The dq tool stays a CLI;
Airflow is the scheduler. There is no Python coupling between the
DAG and warehouse_validation -- BashOperator and exit codes are the
entire interface.

This file is NOT imported by anything in src/. It ships as a
documented example. The repo's CI lints it via `python -m py_compile`
only; it isn't executed (Airflow is not a test dependency).
"""

from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator


EPIC = "1118"
TABLE = "ipn_project"
CONNECTOR = "trino"
EPIC_ROOT = "/opt/airflow/dags/epics"   # adjust for your install


default_args = {
    "owner": "data-quality",
    "depends_on_past": False,
    "retries": 0,
}


with DAG(
    dag_id=f"medallion_validation_{EPIC}_{TABLE}",
    default_args=default_args,
    description=f"Medallion validation for epic {EPIC} table {TABLE}",
    schedule=None,                       # operator-triggered until you wire it
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["data-quality", f"epic-{EPIC}"],
) as dag:

    bronze = BashOperator(
        task_id="validate_bronze",
        bash_command=(
            f"python -m warehouse_validation validate-bronze "
            f"--epic {EPIC} --table {TABLE} "
            f"--connector {CONNECTOR} --epic-root {EPIC_ROOT}"
        ),
    )

    silver = BashOperator(
        task_id="validate_silver",
        bash_command=(
            f"python -m warehouse_validation validate-warehouse "
            f"--epic {EPIC} --table {TABLE} "
            f"--connector {CONNECTOR} --epic-root {EPIC_ROOT}"
        ),
    )

    reconcile = BashOperator(
        task_id="validate_reconcile",
        bash_command=(
            f"python -m warehouse_validation validate-reconcile "
            f"--epic {EPIC} --table {TABLE} "
            f"--connector {CONNECTOR} --epic-root {EPIC_ROOT}"
        ),
    )

    gold = BashOperator(
        task_id="validate_gold",
        bash_command=(
            f"python -m warehouse_validation validate-gold "
            f"--epic {EPIC} --connector {CONNECTOR} --epic-root {EPIC_ROOT}"
        ),
    )

    bronze >> silver >> reconcile >> gold
