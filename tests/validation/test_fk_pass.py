"""Direct tests for `validation.fk_pass.run_cross_table_fk` gating + emission."""

from __future__ import annotations

import polars as pl
import pytest

from dq_core.contract import Contract, FieldContract
from dq_core.gates import Gates, GateSpec
from dq_core.type_mapping import Type
from data_contract.validation.config import TableValidationConfig
from data_contract.validation.fk_pass import run_cross_table_fk
from dq_core.report_models import TableReport


def _parent_contract() -> Contract:
    return Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="PARENT",
        fields=[FieldContract(silver_name="id", type=Type.STRING, nullable=False, description=None, primary_key=True)],
    )


def _child_contract() -> Contract:
    return Contract(
        version="1.0", epic="E", generated_at="", spec_file="", spec_sheet="", table="CHILD",
        fields=[
            FieldContract(
                silver_name="parent_id", type=Type.STRING, nullable=False, description=None,
                foreign_key={"table": "PARENT", "column": "id"},
            ),
        ],
    )


def _gates(fk_existence_enabled: bool) -> Gates:
    return Gates(
        specs={"fk_existence": GateSpec(enabled=fk_existence_enabled)},
        tier_of={"fk_existence": "table"},
    )


def _cfg(table: str, fk_existence: bool) -> TableValidationConfig:
    return TableValidationConfig(
        table=table, format="csv", file_pattern=f"{table}*.csv",
        checks=_gates(fk_existence),
    )


def _report(table: str) -> TableReport:
    return TableReport(table=table, contract_version="1.0", pk_fields=[], input_files=[])


def test_child_gate_off_skips_fk_emission():
    """When the child table disables `fk_existence`, no FK violations emit
    even if the parent's row is missing."""
    parent_frame = pl.DataFrame({"id": ["A"]}).lazy()
    child_frame = pl.DataFrame({
        "parent_id": ["MISSING"],
        "__source_file__": ["c.csv"],
        "__row_index__": [1],
    }).lazy()
    child_report = _report("CHILD")
    run_cross_table_fk(
        table_reports=[child_report],
        table_frames={"PARENT": parent_frame, "CHILD": child_frame},
        table_configs={"CHILD": _cfg("CHILD", fk_existence=False)},
        contracts_by_table={"PARENT": _parent_contract(), "CHILD": _child_contract()},
    )
    assert child_report.violations == []


def test_missing_parent_table_emits_warning():
    """When the parent isn't loaded in this run, a warning surfaces with the
    `fk_target_table_not_loaded` kind."""
    child_frame = pl.DataFrame({
        "parent_id": ["X"],
        "__source_file__": ["c.csv"],
        "__row_index__": [1],
    }).lazy()
    child_report = _report("CHILD")
    run_cross_table_fk(
        table_reports=[child_report],
        table_frames={"CHILD": child_frame},  # parent missing
        table_configs={"CHILD": _cfg("CHILD", fk_existence=True)},
        contracts_by_table={"CHILD": _child_contract()},
    )
    kinds = [(v.kind, v.severity) for v in child_report.violations]
    assert ("fk_target_table_not_loaded", "warning") in kinds


def test_happy_path_emits_one_violation_per_orphan_row():
    """Child rows whose `parent_id` isn't in the parent's PK emit one
    `fk_not_found` violation each."""
    parent_frame = pl.DataFrame({"id": ["A", "B"]}).lazy()
    child_frame = pl.DataFrame({
        "parent_id": ["A", "B", "ORPHAN"],
        "__source_file__": ["c.csv"] * 3,
        "__row_index__": [1, 2, 3],
    }).lazy()
    child_report = _report("CHILD")
    run_cross_table_fk(
        table_reports=[child_report],
        table_frames={"PARENT": parent_frame, "CHILD": child_frame},
        table_configs={"CHILD": _cfg("CHILD", fk_existence=True)},
        contracts_by_table={"PARENT": _parent_contract(), "CHILD": _child_contract()},
    )
    fk_violations = [v for v in child_report.violations if v.kind == "fk_not_found"]
    assert len(fk_violations) == 1
    assert fk_violations[0].offending_value == "ORPHAN"
