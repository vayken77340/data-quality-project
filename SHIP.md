# Ship checklist

Operator-facing steps to take `feature/warehouse_validation` from
"all tests green locally + in CI" to "running against the real
warehouse". Everything Claude could close without warehouse
credentials has been closed; this file covers the rest.

## 1. Trino soak

Install the extras and set env vars:

    pip install -e .[validate-warehouse]
    setx TRINO_HOST    your.trino.host
    setx TRINO_USER    your_user
    setx TRINO_CATALOG your_catalog
    setx TRINO_SCHEMA  your_schema      :: optional
    setx TRINO_PORT    443               :: optional, default 443

Confirm each runner against a known-good table (expect exit 0):

    warehouse-validation validate-bronze     --epic 1118 --table ipn_project --connector trino
    warehouse-validation validate-warehouse  --epic 1118 --table ipn_project --connector trino
    warehouse-validation validate-reconcile  --epic 1118 --table ipn_project --connector trino
    warehouse-validation validate-gold       --epic 1118 --connector trino

Confirm each against a known-bad table (expect exit 2; non-zero =
violations found in the report under
`epics/1118/validations_warehouse/<subdir>/`).

## 2. Oracle soak (skip if your stack does not use Oracle)

    pip install -e .[validate-warehouse-oracle]
    setx ORACLE_USER     your_user
    setx ORACLE_PASSWORD your_password
    setx ORACLE_DSN      host:port/service_name
    setx ORACLE_SCHEMA   your_schema   :: optional; defaults to user

Repeat the four-runner sequence with `--connector oracle`.

## 3. Update epic warehouse.yaml + gold SQL

Replace the placeholder coordinates in
`epics/1118/configs/warehouse.yaml`. The placeholder safety check
will block any runner invocation until you do -- you will see a
`tables.<name>.bronze still uses placeholder catalog/schema names`
ConfigError in stderr.

Same applies to any gold-rule SQL that still references
`placeholder_catalog.placeholder_schema.*`. List them with:

    grep -rn "placeholder_catalog" epics/

Update each `epics/<E>/rules/gold/<rule>.sql` to point at your real
silver tables. The check fires when `validate-gold` loads the rule.

## 4. Airflow DAG drop-in

Copy `airflow_templates/medallion_dag.py` into your Airflow DAGs
folder. Edit the four constants at the top: `EPIC`, `TABLE`,
`CONNECTOR`, `EPIC_ROOT`. `EPIC_ROOT` must point at where your
`epics/<epic>/` tree lives on the Airflow worker.

Confirm Airflow parses the DAG:

    airflow dags list-import-errors

Trigger a manual run. Confirm each task uses the right exit code:
0 -> task green; 2 -> task red and the downstream short-circuits
via the default trigger rule.

## 5. Merge + tag

    git checkout main
    git merge --no-ff feature/warehouse_validation
    git tag -a v0.2.0 -m "Warehouse validation"
    git push origin main --tags

CI will run `python -m pytest tests/ -q` against the matrix
(`.github/workflows/tests.yml`). 1102 tests must pass on each
Python version.
