from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

import yaml

from dq_core.contract import Contract, FieldContract
from data_contract.generation.docs import (
    DRIFT_HEADERS,
    DriftAggregate,
    DriftEntry,
    _format_constraints,
    build_data_dictionary_workbook,
    load_drift_entries,
    write_data_dictionary,
)
from data_contract.generation.joins import JoinsContract, JoinRow
from dq_core.type_mapping import Type


# NOTE: kept local instead of using tests/conftest.py's `contract()` because
# every callsite in this file depends on `generated_at` and `spec_file`
# being the specific values below (asserted by the data-dictionary tests).
# Passing both kwargs at every callsite was rejected in audit v7/v8 -- they
# belong with the factory, not at every call.
def _contract(table: str, *fields: FieldContract) -> Contract:
    return Contract(
        version="1.0",
        epic="E",
        generated_at="2026-06-04T10:00:00Z",
        spec_file="epics/E/specs/spec.xlsx",
        spec_sheet=table,
        table=table,
        fields=list(fields),
    )


def _f(name: str, t: Type = Type.STRING, **kw) -> FieldContract:
    return FieldContract(
        name=name,
        type=t,
        nullable=kw.get("nullable", True),
        description=kw.get("description"),
        max_length=kw.get("max_length"),
        primary_key=kw.get("primary_key"),
        foreign_key=kw.get("foreign_key"),
        constraints=dict(kw.get("constraints", {})),
    )


def test_constraint_formatter_handles_structured_and_flat():
    text = _format_constraints({
        "allowed_values": ["a", "b", "c"],
        "min_value": {"value": 0, "strict": False},
        "max_value": {"value": 100, "strict": True},
        "pattern": "^[a-z]+$",
        "unique": True,
    })
    # Sorted by key: allowed_values, max_value, min_value, pattern, unique
    assert "allowed_values: [a, b, c]" in text
    assert "min_value: 0 (>=)" in text
    assert "max_value: 100 (<)" in text
    assert "pattern: ^[a-z]+$" in text
    assert "unique" in text


def test_workbook_has_readme_table_and_joins_sheets():
    proj = _contract(
        "PROJECT",
        _f("proj_id", t=Type.INT64, nullable=False, primary_key=True, description="ID"),
        _f("status", constraints={"allowed_values": ["a", "b"]}),
    )
    cal = _contract(
        "CALENDAR",
        _f("test_id", t=Type.INT64, nullable=False, primary_key=True),
        _f("proj_id", foreign_key={"table": "PROJECT", "column": "proj_id"}, nullable=False),
    )
    joins = JoinsContract(
        version="1.0",
        epic="E",
        generated_at="2026-06-04T10:00:00Z",
        spec_file="epics/E/specs/spec.xlsx",
        spec_sheet="joins",
        joins=[JoinRow(
            sheet_row=2,
            source_table="PROJECT", source_column="proj_id",
            target_table="CALENDAR", target_column="proj_id",
            join_type="LEFT", cardinality="1:n",
            comment="Une WBS appartient a un projet",
            description="biz rule",
        )],
    )
    wb = build_data_dictionary_workbook(
        epic="E", version="1.0",
        generated_at="2026-06-04T10:00:00Z",
        spec_file="epics/E/specs/spec.xlsx",
        contracts=[proj, cal], joins=joins,
    )

    assert wb.sheetnames == ["README", "CALENDAR", "PROJECT", "Joins"]

    # README content
    readme = wb["README"]
    readme_values = {readme.cell(row=r, column=1).value: readme.cell(row=r, column=2).value
                     for r in range(1, readme.max_row + 1)
                     if readme.cell(row=r, column=1).value}
    assert readme_values["Epic"] == "E"
    assert readme_values["Version"] == "1.0"
    assert readme_values["Tables"] == 2
    assert readme_values["Joins"] == 1

    # PROJECT sheet header + a sample row.
    proj_ws = wb["PROJECT"]
    headers = [proj_ws.cell(row=1, column=c).value for c in range(1, proj_ws.max_column + 1)]
    assert headers[0] == "Field" and headers[-1] == "Constraints"
    name_col = {proj_ws.cell(row=r, column=1).value: r for r in range(2, proj_ws.max_row + 1)}
    status_row = name_col["status"]
    constraints_cell = proj_ws.cell(row=status_row, column=len(headers)).value
    assert "allowed_values" in constraints_cell
    proj_id_row = name_col["proj_id"]
    assert proj_ws.cell(row=proj_id_row, column=7).value == "yes"  # Primary Key column

    # CALENDAR FK formatted as table.column.
    cal_ws = wb["CALENDAR"]
    cal_names = {cal_ws.cell(row=r, column=1).value: r for r in range(2, cal_ws.max_row + 1)}
    fk_cell = cal_ws.cell(row=cal_names["proj_id"], column=8).value  # Foreign Key col
    assert fk_cell == "PROJECT.proj_id"

    # Joins sheet rows.
    joins_ws = wb["Joins"]
    assert joins_ws.cell(row=1, column=1).value == "Source Table"
    assert joins_ws.cell(row=2, column=1).value == "PROJECT"
    assert joins_ws.cell(row=2, column=5).value == "LEFT"
    assert joins_ws.cell(row=2, column=6).value == "1:n"


def test_write_data_dictionary_creates_file(tmp_path: Path):
    contract = _contract(
        "T",
        _f("x", t=Type.INT64, nullable=False, primary_key=True),
    )
    out = write_data_dictionary(
        epic_dir=tmp_path / "epics" / "E",
        epic="E",
        version="1.0",
        spec_file="spec.xlsx",
        contracts=[contract],
        joins=None,
    )
    assert out.exists()
    assert out == tmp_path / "epics" / "E" / "docs" / "data_dictionary.xlsx"
    wb = load_workbook(out, read_only=True)
    assert "T" in wb.sheetnames
    assert "Joins" not in wb.sheetnames
    wb.close()


def test_safe_sheet_name_strips_invalid_characters():
    from data_contract.generation.docs import _safe_sheet_name
    assert _safe_sheet_name("A/B:C") == "A_B_C"
    long_name = "X" * 50
    assert len(_safe_sheet_name(long_name)) == 31


# ---------------------------------------------------------------------------
# Drift sheet
# ---------------------------------------------------------------------------


def _write_drift_yaml(path: Path, *, from_v: str, to_v: str, table: str, changes: list[dict]) -> None:
    summary = {"breaking": 0, "additive": 0, "cosmetic": 0}
    for c in changes:
        summary[c["severity"]] = summary.get(c["severity"], 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "table": table,
        "from_version": from_v,
        "to_version": to_v,
        "generated_at": "t",
        "summary": summary,
        "changes": changes,
    }, sort_keys=False), encoding="utf-8")


def test_load_drift_entries_empty_dir_returns_empty_aggregate(tmp_path: Path):
    agg = load_drift_entries(tmp_path)
    assert agg.total == 0 and agg.breaking == 0 and agg.additive == 0


def test_load_drift_entries_aggregates_files(tmp_path: Path):
    drift_dir = tmp_path / "drift"
    _write_drift_yaml(
        drift_dir / "PROJECT__v1.0_to_v2.0.yaml",
        from_v="1.0", to_v="2.0", table="PROJECT",
        changes=[
            {"kind": "field_added", "severity": "additive", "field": "tag"},
            {"kind": "nullable_tightened", "severity": "breaking", "field": "id"},
        ],
    )
    _write_drift_yaml(
        drift_dir / "CALENDAR__v1.0_to_v1.10.yaml",
        from_v="1.0", to_v="1.10", table="CALENDAR",
        changes=[{"kind": "description_changed", "severity": "cosmetic", "field": "day"}],
    )
    agg = load_drift_entries(tmp_path)
    assert agg.total == 3
    assert agg.breaking == 1 and agg.additive == 1 and agg.cosmetic == 1
    # to_version sort key: 2.0 (PROJECT entries) before 1.10 (CALENDAR).
    assert agg.entries[0].to_version == "2.0"
    assert agg.entries[-1].to_version == "1.10"


def test_drift_sheet_present_when_drift_exists(tmp_path: Path):
    entries = [
        DriftEntry(from_version="1.0", to_version="2.0", table="T", field="x",
                   kind="field_added", severity="additive",
                   from_value=None, to_value="integer"),
    ]
    agg = DriftAggregate(entries=entries, breaking=0, additive=1, cosmetic=0)
    wb = build_data_dictionary_workbook(
        epic="E", version="2.0", generated_at="t",
        spec_file="spec.xlsx",
        contracts=[_contract("T", _f("x"))],
        joins=None,
        drift=agg,
    )
    assert "Drift" in wb.sheetnames
    ws = wb["Drift"]
    assert tuple(ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)) == DRIFT_HEADERS
    assert ws.cell(row=2, column=1).value == "1.0"
    assert ws.cell(row=2, column=2).value == "2.0"
    assert ws.cell(row=2, column=5).value == "field_added"


def test_drift_sheet_omitted_when_no_drift():
    wb = build_data_dictionary_workbook(
        epic="E", version="1.0", generated_at="t",
        spec_file="spec.xlsx",
        contracts=[_contract("T", _f("x"))],
        joins=None,
        drift=None,
    )
    assert "Drift" not in wb.sheetnames


def test_drift_sheet_omitted_when_aggregate_is_empty():
    agg = DriftAggregate(entries=[], breaking=0, additive=0, cosmetic=0)
    wb = build_data_dictionary_workbook(
        epic="E", version="1.0", generated_at="t",
        spec_file="spec.xlsx",
        contracts=[_contract("T", _f("x"))],
        joins=None,
        drift=agg,
    )
    assert "Drift" not in wb.sheetnames


def test_readme_omits_drift_counts():
    """Drift totals belong on the Drift sheet itself, not in the README."""
    entries = [
        DriftEntry(from_version="1.0", to_version="2.0", table="T", field=None,
                   kind="field_removed", severity="breaking",
                   from_value="integer", to_value=None),
    ]
    agg = DriftAggregate(entries=entries, breaking=1, additive=0, cosmetic=0)
    wb = build_data_dictionary_workbook(
        epic="E", version="2.0", generated_at="t",
        spec_file="spec.xlsx",
        contracts=[_contract("T", _f("x"))],
        joins=None,
        drift=agg,
    )
    readme = wb["README"]
    rows = {readme.cell(row=r, column=1).value: readme.cell(row=r, column=2).value
            for r in range(1, readme.max_row + 1)
            if readme.cell(row=r, column=1).value}
    for label in ("Drift entries", "Breaking", "Additive", "Cosmetic"):
        assert label not in rows


def test_readme_omits_joins_row_when_no_joins_contract():
    wb = build_data_dictionary_workbook(
        epic="E", version="1.0", generated_at="t",
        spec_file="spec.xlsx",
        contracts=[_contract("T", _f("x"))],
        joins=None,
    )
    readme = wb["README"]
    labels = {readme.cell(row=r, column=1).value
              for r in range(1, readme.max_row + 1)
              if readme.cell(row=r, column=1).value}
    assert "Joins" not in labels
