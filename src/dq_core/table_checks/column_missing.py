"""Column-missing schema check.

Verifies every contract-declared field has a corresponding column in the
data file. Table-scope: emits one Violation per missing column, with no
source row (it's a structural / schema gap).

Migrated from the inline block in `validation/runner.py` so devs can
turn this off / replace it like any other table check.
"""

from __future__ import annotations

from dq_core.table_checks.base import TableCheck
from dq_core.violations import Violation


class ColumnMissingCheck(TableCheck):
    name = "column_missing"
    description = "Verify every contract-declared column exists in the data file."
    VIOLATION_KIND = "column_missing"
    DIMENSION = "completeness"
    scope = "table"

    def check_data(
        self,
        frame,
        contract,
        *,
        data_columns=None,
        contracts_by_table=None,
        table_frames=None,
    ):
        if data_columns is None:
            return None
        contract_field_names = contract.field_name_set()
        missing = sorted(contract_field_names - data_columns)
        if not missing:
            return None
        return [
            Violation(
                kind=self.VIOLATION_KIND,
                severity=self.VIOLATION_SEVERITY,
                table=contract.table,
                field=fname,
                expected=f"data file must contain column {fname!r}",
            )
            for fname in missing
        ]
