"""Per-(table, check) status aggregation.

Builds the tables x checks grid surfaced at the top of every report
format. For each enabled check, reports a row-count breakdown:

* row-scope checks    -- pass / warning / error are distinct
                          (source_file, source_row) tuples affected by
                          that check's `VIOLATION_KIND`. `pass_rows` is
                          `total_rows - (error + warning)`.
* table-scope checks  -- there is no per-row notion of pass; `pass_rows`
                          is `None` and `violation_count` is the raw
                          number of occurrences (e.g. "3 missing
                          columns"). Status is OK / WARNING / ERROR
                          based on severity present.

Operational kinds (`no_input_files`, `parser_failure`) short-circuit:
when present, every row-scope check is `NA` (the data couldn't be loaded
so the per-row counts are meaningless).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# Kinds with no source-row attached. Either pure schema (column_missing)
# or operational environment failures. The grid renders them differently:
# pass_rows is None, status uses violation_count not row counts.
TABLE_LEVEL_KINDS: frozenset[str] = frozenset({
    "column_missing",
    "no_input_files",
    "parser_failure",
    "fk_target_table_not_loaded",
    "extra_column",
})

# Operational kinds that mean "we couldn't even load the data". When any
# of these are present, row-scope checks short-circuit to NA.
_LOAD_BLOCKING_KINDS: frozenset[str] = frozenset({
    "no_input_files",
    "parser_failure",
})


# Status sentinels surfaced in the grid.
STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"   # check disabled in this run
STATUS_NA = "NA"             # couldn't be evaluated (operational short-circuit)


@dataclass(frozen=True)
class CheckStatus:
    """One cell in the tables x checks grid.

    `scope`:
      - `"row"`   -- pass_rows, warning_rows, error_rows populated.
      - `"table"` -- pass_rows is None; violation_count populated.

    `status` is one of STATUS_OK / WARNING / ERROR / SKIPPED / NA.
    """
    check_name: str
    status: str
    scope: str                          # "row" | "table"
    pass_rows: int | None
    warning_rows: int
    error_rows: int
    violation_count: int                # raw count -- redundant for row scope, primary for table scope


def compute_table_check_status(
    *,
    total_rows: int,
    violations: Iterable,
    enabled_checks: dict[str, bool],
    check_to_kind: dict[str, str],
) -> dict[str, CheckStatus]:
    """Build the per-check CheckStatus for one table.

    `enabled_checks` maps check_name -> bool. Names NOT in this dict are
    treated as enabled (defensive; production loads guarantee the dict is
    exhaustive).

    `check_to_kind` is the check-name -> VIOLATION_KIND mapping (from
    `dimensions.check_to_violation_kind()`). The check is identified by
    its name; the kind is what's stamped on each Violation.
    """
    violations = list(violations)
    operational_short_circuit = any(
        getattr(v, "kind", None) in _LOAD_BLOCKING_KINDS for v in violations
    )

    by_kind_error: dict[str, set[tuple]] = {}
    by_kind_warn: dict[str, set[tuple]] = {}
    by_kind_table_count: dict[str, dict[str, int]] = {}   # kind -> {severity -> count}

    for v in violations:
        kind = getattr(v, "kind", None)
        if not kind:
            continue
        severity = getattr(v, "severity", "error")
        source_file = getattr(v, "source_file", None)
        source_row = getattr(v, "source_row", None)

        is_table_level = (
            kind in TABLE_LEVEL_KINDS
            or source_file is None
            or source_row is None
        )

        if is_table_level:
            bucket = by_kind_table_count.setdefault(kind, {})
            bucket[severity] = bucket.get(severity, 0) + 1
            continue

        key = (source_file, source_row)
        if severity == "error":
            by_kind_error.setdefault(kind, set()).add(key)
        elif severity == "warning":
            by_kind_warn.setdefault(kind, set()).add(key)
        # info severity ignored for the grid: it shouldn't tank the status

    out: dict[str, CheckStatus] = {}
    for check_name, kind in check_to_kind.items():
        enabled = enabled_checks.get(check_name, True)
        if not enabled:
            out[check_name] = CheckStatus(
                check_name=check_name,
                status=STATUS_SKIPPED,
                scope="row" if kind not in TABLE_LEVEL_KINDS else "table",
                pass_rows=None,
                warning_rows=0,
                error_rows=0,
                violation_count=0,
            )
            continue

        if kind in TABLE_LEVEL_KINDS:
            sev_counts = by_kind_table_count.get(kind, {})
            errors = sev_counts.get("error", 0)
            warns = sev_counts.get("warning", 0)
            total = sum(sev_counts.values())
            if errors:
                status = STATUS_ERROR
            elif warns:
                status = STATUS_WARNING
            else:
                status = STATUS_OK
            out[check_name] = CheckStatus(
                check_name=check_name,
                status=status,
                scope="table",
                pass_rows=None,
                warning_rows=warns,
                error_rows=errors,
                violation_count=total,
            )
            continue

        # row scope
        if operational_short_circuit:
            out[check_name] = CheckStatus(
                check_name=check_name,
                status=STATUS_NA,
                scope="row",
                pass_rows=None,
                warning_rows=0,
                error_rows=0,
                violation_count=0,
            )
            continue

        err_keys = by_kind_error.get(kind, set())
        warn_keys = by_kind_warn.get(kind, set()) - err_keys
        error_rows = len(err_keys)
        warning_rows = len(warn_keys)
        pass_rows = max(total_rows - error_rows - warning_rows, 0)
        if error_rows:
            status = STATUS_ERROR
        elif warning_rows:
            status = STATUS_WARNING
        else:
            status = STATUS_OK
        out[check_name] = CheckStatus(
            check_name=check_name,
            status=status,
            scope="row",
            pass_rows=pass_rows,
            warning_rows=warning_rows,
            error_rows=error_rows,
            violation_count=error_rows + warning_rows,
        )

    return out
