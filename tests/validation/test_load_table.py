"""Direct tests for `validation.load_table.load_table`."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from data_contract.contract import Contract, FieldContract
from data_contract.settings import Settings
from data_contract.type_mapping import Type, load_type_registry
from data_contract.validation.config import ValidationConfig
from data_contract.validation.load_table import LoadedTable, load_table
from data_contract.validation.models import TableReport
from tests.conftest import (
    ALL_CHECKS_ENABLED_YAML, write_test_parsers_yaml,
)


def _build_config(tmp_path: Path, file_pattern: str = "{table}*.csv") -> ValidationConfig:
    """Minimal ValidationConfig backed by a tmp configs dir."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    write_test_parsers_yaml(cfg_dir)
    (cfg_dir / "validation.yaml").write_text(
        f'defaults:\n  format: csv\n  file_pattern: "{file_pattern}"\n' +
        ALL_CHECKS_ENABLED_YAML,
        encoding="utf-8",
    )
    return ValidationConfig.from_yaml(cfg_dir / "validation.yaml", cfg_dir / "parsers.yaml")


# NOTE: kept local instead of using tests/conftest.py's `contract()` because
# this helper is a fixture-builder that bakes in the specific (id, name)
# field shape every load-table test asserts on. Pulling the fields out and
# passing them at every callsite was rejected in audit v7/v8 -- the
# hardcoded shape is the test's contract, not a parameter.
def _contract(table: str = "T") -> Contract:
    return Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="",
        table=table, fields=[
            FieldContract(name="id", type=Type.STRING, nullable=False, description=None),
            FieldContract(name="name", type=Type.STRING, nullable=True, description=None),
        ],
    )


def _report(table: str = "T") -> TableReport:
    return TableReport(table=table, contract_version="1.0", pk_fields=["id"], input_files=[])


@pytest.fixture
def registry(types_yaml_path: Path):
    return load_type_registry(types_yaml_path)


def test_happy_path_returns_loaded_table(tmp_path: Path, registry):
    """A matching CSV with hermetic defaults loads cleanly."""
    config = _build_config(tmp_path)
    table_cfg = config.build_table_entry("T")
    contract = _contract()
    report = _report()
    sample_dir = tmp_path / "input"
    sample_dir.mkdir()
    (sample_dir / "T.csv").write_text("id,name\n1,alice\n2,bob\n", encoding="utf-8")

    loaded = load_table(
        config=config, table_cfg=table_cfg, contract=contract,
        input_dir=sample_dir, report=report,
        strict_columns=False, settings=Settings(),
    )

    assert isinstance(loaded, LoadedTable)
    assert loaded.df.height == 2
    assert {"id", "name"} <= loaded.data_columns
    assert report.total_rows == 2


def test_no_input_files_emits_violation_and_returns_none(tmp_path: Path, registry):
    """Glob with no matches surfaces a `no_input_files` violation; returns None."""
    config = _build_config(tmp_path)
    table_cfg = config.build_table_entry("T")
    contract = _contract()
    report = _report()
    (tmp_path / "input").mkdir()

    loaded = load_table(
        config=config, table_cfg=table_cfg, contract=contract,
        input_dir=tmp_path / "input", report=report,
        strict_columns=False, settings=Settings(),
    )

    assert loaded is None
    kinds = [v.kind for v in report.violations]
    assert "no_input_files" in kinds


def test_extra_column_surfaced_as_warning_by_default(tmp_path: Path, registry):
    """An undeclared column in the CSV becomes a warning unless strict_columns."""
    config = _build_config(tmp_path)
    table_cfg = config.build_table_entry("T")
    contract = _contract()
    report = _report()
    sample_dir = tmp_path / "input"
    sample_dir.mkdir()
    (sample_dir / "T.csv").write_text("id,name,extra\n1,a,x\n", encoding="utf-8")

    load_table(
        config=config, table_cfg=table_cfg, contract=contract,
        input_dir=sample_dir, report=report,
        strict_columns=False, settings=Settings(),
    )
    extra_cols = [v for v in report.violations if v.kind == "extra_column"]
    assert extra_cols
    assert all(v.severity == "warning" for v in extra_cols)


def test_strict_columns_promotes_extra_column_to_error(tmp_path: Path, registry):
    """`strict_columns=True` flips the severity to error."""
    config = _build_config(tmp_path)
    table_cfg = config.build_table_entry("T")
    contract = _contract()
    report = _report()
    sample_dir = tmp_path / "input"
    sample_dir.mkdir()
    (sample_dir / "T.csv").write_text("id,name,extra\n1,a,x\n", encoding="utf-8")

    load_table(
        config=config, table_cfg=table_cfg, contract=contract,
        input_dir=sample_dir, report=report,
        strict_columns=True, settings=Settings(),
    )
    extra_cols = [v for v in report.violations if v.kind == "extra_column"]
    assert extra_cols
    assert all(v.severity == "error" for v in extra_cols)
