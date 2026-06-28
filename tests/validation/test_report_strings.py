"""Tests for the externalized report-strings loader.

Ensures:
  - The YAML loads.
  - Every key path the XLSX and Markdown writers reference is present.
  - The templated strings substitute correctly.
"""

from __future__ import annotations

import pytest

from dq_core.report.strings import ReportStrings, load_strings


# Every key path the XLSX writer reads, in (path-tuple, expected-leaf-type) form.
_XLSX_KEY_PATHS: list[tuple[tuple[str, ...], type]] = [
    (("sheets", "run"), str),
    (("sheets", "summary"), str),
    (("sheets", "profile"), str),
    (("sheets", "checks"), str),
    (("sheets", "run_issues"), str),
    (("sheets", "rejected_pattern"), str),
    (("status", "pass"), str),
    (("status", "fail"), str),
    (("run_sheet", "labels"), dict),
    # Run-sheet label keys that must remain after we dropped tool_version,
    # target_description, and types_yaml.
    (("run_sheet", "labels", "epic"), str),
    (("run_sheet", "labels", "target"), str),
    (("run_sheet", "labels", "cli_args"), str),
    (("run_sheet", "no_target"), str),
    (("run_sheet", "contract_versions_heading"), str),
    (("summary_sheet", "overall_labels"), dict),
    (("summary_sheet", "scorecard_headers"), list),
    (("summary_sheet", "no_pk_placeholder"), str),
    (("summary_sheet", "missing_score_placeholder"), str),
    (("profile_sheet", "headers"), list),
    (("profile_sheet", "table_heading_template"), str),
    (("profile_sheet", "pk_indicator"), str),
    (("profile_sheet", "fk_indicator"), str),
    (("checks_sheet", "headers"), list),
    (("rejected_sheet", "headers"), dict),
    (("rejected_sheet", "truncated_template"), str),
    (("run_issues_sheet", "headers"), list),
    (("run_issues_sheet", "field_placeholder"), str),
]

_MARKDOWN_KEY_PATHS: list[tuple[tuple[str, ...], type]] = [
    (("markdown", "title_template"), str),
    (("markdown", "status_pass"), str),
    (("markdown", "status_fail"), str),
    (("markdown", "headline_template"), str),
    (("markdown", "target_template"), str),
    (("markdown", "target_with_disabled_template"), str),
    (("markdown", "no_target_with_disabled_template"), str),
    (("markdown", "dimensions_table_header"), str),
    (("markdown", "dimensions_table_separator"), str),
    (("markdown", "dimensions_row_template"), str),
    (("markdown", "tables_section"), str),
    (("markdown", "tables_table_header"), str),
    (("markdown", "tables_table_separator"), str),
    (("markdown", "tables_row_template"), str),
    (("markdown", "top_issues_section"), str),
    (("markdown", "no_issues"), str),
    (("markdown", "issue_with_field_template"), str),
    (("markdown", "issue_without_field_template"), str),
    (("markdown", "footer"), str),
]


def _all_paths() -> list[tuple[tuple[str, ...], type]]:
    return _XLSX_KEY_PATHS + _MARKDOWN_KEY_PATHS


@pytest.mark.parametrize("path,expected_type", _all_paths())
def test_every_key_path_resolves(path, expected_type):
    S = load_strings()
    value = S.get(*path)
    assert isinstance(value, expected_type), (
        f"{'.'.join(path)} resolved to {type(value).__name__}, "
        f"expected {expected_type.__name__}"
    )


def test_missing_key_raises_keyerror():
    S = load_strings()
    with pytest.raises(KeyError):
        S.get("sheets", "definitely_not_a_key")


def test_fmt_substitutes_placeholders():
    S = load_strings()
    out = S.fmt("sheets", "rejected_pattern", table="customers")
    assert out == "customers_rejected"


def test_fmt_with_multiple_placeholders():
    S = load_strings()
    out = S.fmt(
        "markdown", "headline_template",
        status="**FAIL**", score=87.3, errors=12, warnings=4,
    )
    assert "**FAIL**" in out
    assert "87.3" in out
    assert "12 errors" in out
    assert "4 warnings" in out


def test_fmt_on_nontemplate_raises():
    """`fmt` on a key that resolves to a dict/list (not a string) is a bug."""
    S = load_strings()
    with pytest.raises(TypeError):
        S.fmt("sheets")


def test_truncated_template_uses_count_placeholder():
    S = load_strings()
    msg = S.fmt("rejected_sheet", "truncated_template", count=42)
    assert "42" in msg


def test_markdown_title_template():
    S = load_strings()
    out = S.fmt("markdown", "title_template",
                epic="1118", generated_at="2026-06-09T10:00:00Z")
    assert "1118" in out
    assert "2026-06-09T10:00:00Z" in out


def test_construct_from_dict_directly():
    S = ReportStrings({"a": {"b": "hello {name}"}})
    assert S.get("a", "b") == "hello {name}"
    assert S.fmt("a", "b", name="world") == "hello world"
