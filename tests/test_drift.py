from data_contract.contract import Contract, FieldContract
from data_contract.drift import diff_contracts
from data_contract.type_mapping import Type


def _c(version, fields):
    return Contract(
        version=version,
        epic="E",
        generated_at="2026-06-02T14:00:00Z",
        spec_file="x.xlsx",
        spec_sheet="T",
        table="T",
        fields=fields,
    )


def _f(name, t=Type.STRING, nullable=True, description=None, max_length=None,
       primary_key=None, foreign_key=None, constraints=None):
    return FieldContract(
        name=name,
        type=t,
        nullable=nullable,
        description=description,
        max_length=max_length,
        primary_key=primary_key,
        foreign_key=foreign_key,
        constraints=dict(constraints or {}),
    )


def test_empty_when_identical():
    a = _c("1.0", [_f("x")])
    b = _c("2.0", [_f("x")])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert report.is_empty()


def test_field_added_required_is_breaking():
    a = _c("1.0", [_f("x")])
    b = _c("2.0", [_f("x"), _f("y", nullable=False)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    kinds = {(c.kind, c.severity, c.field) for c in report.changes}
    assert ("field_added", "breaking", "y") in kinds


def test_field_added_nullable_is_additive():
    a = _c("1.0", [_f("x")])
    b = _c("2.0", [_f("x"), _f("y", nullable=True)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    kinds = {(c.kind, c.severity, c.field) for c in report.changes}
    assert ("field_added", "additive", "y") in kinds


def test_field_removed_is_breaking():
    a = _c("1.0", [_f("x"), _f("y")])
    b = _c("2.0", [_f("x")])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    kinds = {(c.kind, c.severity, c.field) for c in report.changes}
    assert ("field_removed", "breaking", "y") in kinds


def test_field_type_changed_is_breaking():
    a = _c("1.0", [_f("x", t=Type.STRING)])
    b = _c("2.0", [_f("x", t=Type.INTEGER)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "field_type_changed" and c.severity == "breaking" for c in report.changes)


def test_nullable_tightened_is_breaking():
    a = _c("1.0", [_f("x", nullable=True)])
    b = _c("2.0", [_f("x", nullable=False)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "nullable_tightened" and c.severity == "breaking" for c in report.changes)


def test_nullable_relaxed_is_additive():
    a = _c("1.0", [_f("x", nullable=False)])
    b = _c("2.0", [_f("x", nullable=True)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "nullable_relaxed" and c.severity == "additive" for c in report.changes)


def test_max_length_tightened_is_breaking():
    a = _c("1.0", [_f("x", max_length=255)])
    b = _c("2.0", [_f("x", max_length=100)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "max_length_tightened" and c.severity == "breaking" for c in report.changes)


def test_max_length_relaxed_is_additive():
    a = _c("1.0", [_f("x", max_length=100)])
    b = _c("2.0", [_f("x", max_length=500)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "max_length_relaxed" and c.severity == "additive" for c in report.changes)


def test_description_change_is_cosmetic():
    a = _c("1.0", [_f("x", description="old")])
    b = _c("2.0", [_f("x", description="new")])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "description_changed" and c.severity == "cosmetic" for c in report.changes)


def test_primary_key_added_or_removed_is_breaking():
    """primary_key transition true -> None (and back) is breaking via the
    named-field diff branch."""
    a = _c("1.0", [_f("x", primary_key=True)])
    b = _c("2.0", [_f("x")])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "primary_key_changed" and c.severity == "breaking" for c in report.changes)


def test_foreign_key_added_is_breaking():
    a = _c("1.0", [_f("x")])
    b = _c("2.0", [_f("x", foreign_key={"table": "USERS", "column": "x"})])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "foreign_key_added" and c.severity == "breaking" for c in report.changes)


def test_foreign_key_removed_is_additive():
    a = _c("1.0", [_f("x", foreign_key={"table": "USERS", "column": "x"})])
    b = _c("2.0", [_f("x")])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "foreign_key_removed" and c.severity == "additive" for c in report.changes)


def test_foreign_key_changed_is_breaking():
    a = _c("1.0", [_f("x", foreign_key={"table": "USERS", "column": "x"})])
    b = _c("2.0", [_f("x", foreign_key={"table": "ACCOUNTS", "column": "x"})])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "foreign_key_changed" and c.severity == "breaking" for c in report.changes)


def test_constraint_diff_still_dispatched_for_unknown_keys():
    """A contract carrying an unknown key in `constraints` (e.g. a stale custom
    constraint that was deregistered) still triggers a fallback diff."""
    a = _c("1.0", [_f("x", constraints={"deregistered": "old"})])
    b = _c("2.0", [_f("x", constraints={"deregistered": "new"})])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    assert any(c.kind == "deregistered_changed" for c in report.changes)


def test_summary_counts():
    a = _c("1.0", [_f("x", nullable=False, description="old"), _f("z")])
    b = _c("2.0", [_f("x", nullable=True, description="new"), _f("y", nullable=False)])
    report = diff_contracts(a, b, now="2026-06-02T14:00:00Z")
    s = report.summary()
    # nullable_relaxed (additive), description_changed (cosmetic),
    # field_removed z (breaking), field_added y required (breaking)
    assert s["breaking"] == 2
    assert s["additive"] == 1
    assert s["cosmetic"] == 1


def test_to_dict_shape():
    a = _c("1.0", [_f("x")])
    b = _c("2.0", [_f("x"), _f("y", nullable=True)])
    payload = diff_contracts(a, b, now="2026-06-02T14:00:00Z").to_dict()
    assert payload["table"] == "T"
    assert payload["from_version"] == "1.0"
    assert payload["to_version"] == "2.0"
    assert payload["summary"] == {"breaking": 0, "additive": 1, "cosmetic": 0}
    assert payload["changes"][0]["kind"] == "field_added"
