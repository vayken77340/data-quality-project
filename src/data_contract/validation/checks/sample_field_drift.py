"""Source-schema drift checks.

Two optional structural-tier checks that compare the parser-discovered
source schema (`ParserSchema` exposed by `FileParser.parse_file`) with the
contract:

* `field_names_from_sample` -- detect when the source file's column names
  don't match the contract's field names. Both directions are flagged:
  source has a name the contract doesn't (potential rename upstream) and
  contract has a name no source file declared (drift the other way).

* `field_types_from_sample` -- detect when the source file's declared types
  (e.g. JSON `report_header`'s `"type"` field, Parquet's Arrow schema)
  don't match the contract's declared types after normalisation via the
  parser's `normalize_source_type`.

Both checks emit `severity="warning"` violations and are skipped
silently when no source file in the run reported the relevant schema
component (so CSV runs never trigger `field_types_from_sample` -- CSV
doesn't carry types).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dq_core.violations import Violation

if TYPE_CHECKING:
    from dq_core.contract import Contract
    from data_contract.data_parsers.base import ParserSchema, FileParser


def check_field_names_from_sample(
    *,
    table: str,
    per_file_schemas: "list[tuple[str, ParserSchema | None]]",
    contract: "Contract",
) -> list[Violation]:
    """Compare contract field names against the union of source-declared
    names across every file in this run.

    Skips silently when no file reported `column_names` (the parser
    format doesn't carry them; nothing to compare).
    """
    union_names: set[str] = set()
    any_reported = False
    per_file_set: dict[str, set[str]] = {}
    for filename, schema in per_file_schemas:
        if schema is None or schema.column_names is None:
            continue
        any_reported = True
        names = set(schema.column_names)
        per_file_set[filename] = names
        union_names |= names

    if not any_reported:
        return []

    contract_names = contract.field_name_set()
    out: list[Violation] = []

    # Names in the source files that aren't in the contract.
    for source_only in sorted(union_names - contract_names):
        # Identify which file(s) introduced this name for the message.
        sources = sorted(
            f for f, names in per_file_set.items() if source_only in names
        )
        out.append(Violation(
            kind="field_name_drift",
            severity="warning",
            table=table,
            field=source_only,
            expected="declared by the contract",
            offending_value={
                "direction": "source_only",
                "source_files": sources,
            },
        ))

    # Names in the contract that no source file declared.
    for contract_only in sorted(contract_names - union_names):
        out.append(Violation(
            kind="field_name_drift",
            severity="warning",
            table=table,
            field=contract_only,
            expected="declared by at least one source file's header",
            offending_value={
                "direction": "contract_only",
                "source_files": sorted(per_file_set),
            },
        ))

    return out


def check_field_types_from_sample(
    *,
    table: str,
    per_file_schemas: "list[tuple[str, ParserSchema | None]]",
    contract: "Contract",
    parser: "FileParser",
) -> list[Violation]:
    """Compare contract field types against source-declared types after
    normalisation via `parser.normalize_source_type`.

    Skips silently when no file reported `column_types` (e.g. CSV runs
    -- CSV doesn't carry types).
    """
    # Bail early if no file reported types.
    has_types = any(
        sch is not None and sch.column_types
        for _, sch in per_file_schemas
    )
    if not has_types:
        return []

    contract_types = {f.name: f.type.value for f in contract.fields}
    out: list[Violation] = []
    seen: set[tuple[str, str, str]] = set()   # (field, source_type, contract_type) dedup across files

    for filename, schema in per_file_schemas:
        if schema is None or not schema.column_types:
            continue
        for col_name, source_type in schema.column_types.items():
            contract_type = contract_types.get(col_name)
            if contract_type is None:
                # Column not in contract -- `field_names_from_sample` (if
                # enabled) flags the name; we don't double-report on
                # types here.
                continue
            normalised = parser.normalize_source_type(source_type)
            if normalised is None:
                key = (col_name, source_type, contract_type)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Violation(
                    kind="field_type_unknown",
                    severity="info",
                    table=table,
                    field=col_name,
                    expected=(
                        f"source type {source_type!r} known to "
                        f"{parser.name}'s SOURCE_TYPE_ALIASES"
                    ),
                    offending_value={
                        "source_type": source_type,
                        "contract_type": contract_type,
                        "source_files": [filename],
                    },
                ))
                continue
            if normalised != contract_type:
                key = (col_name, source_type, contract_type)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Violation(
                    kind="field_type_drift",
                    severity="warning",
                    table=table,
                    field=col_name,
                    expected=f"contract type {contract_type!r}",
                    offending_value={
                        "source_type": source_type,
                        "normalised_to": normalised,
                        "contract_type": contract_type,
                        "source_files": [filename],
                    },
                ))

    return out
