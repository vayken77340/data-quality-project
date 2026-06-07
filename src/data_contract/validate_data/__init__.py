"""Data validation subpackage.

Validates sample data files (CSV, Excel; more formats later) against the
contracts produced by `data_contract`. Heavy dependencies (Polars, pyarrow,
fastexcel) are loaded lazily so importing the base `data_contract` package
remains lightweight.

Install the extras to use this subpackage:

    pip install data-contract[validate-data]

CLI entry point: `python -m data_contract validate-data --epic <E> --input-dir <DIR>`
"""
