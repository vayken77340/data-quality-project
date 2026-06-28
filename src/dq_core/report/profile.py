"""Per-field metadata view used by the Profile report section.

Carries the per-field metadata that callers can't derive from the
contract alone -- specifically the target-resolved physical type and the
target name. The contract speaks in universal logical types; only after
target overlay can we say "this `string(20)` becomes `VARCHAR2(20 BYTE)`
under oracle". That resolution lives here.

Per-field descriptive statistics (null count / null %, distinct values)
are NOT carried here -- they come from the metrics registry instead, so
there's a single source of truth for those numbers. Renderers read them
from `TableReport.metrics["null_count"|"null_percentage"|"distinct_count"]`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dq_core.contract import Contract, FieldContract
from dq_core.type_mapping import Type, TypeRegistry


@dataclass(frozen=True)
class FieldProfile:
    """Per-field metadata surfaced in the Profile report section.

    Columns: Field, Type, Type Format, Primary Key, Foreign Key, Total.
    Null # / Null % / Distinct columns come from `TableReport.metrics`.
    """
    name: str
    type: str                         # target's physical_type, e.g. "VARCHAR2(384 BYTE)"
    type_format: str                  # active target's name, e.g. "oracle"
    is_pk: bool                       # rendered as "Primary Key"
    is_fk: bool                       # rendered as "Foreign Key"
    total: int                        # total rows in the table (denominator for % metrics)


@dataclass(frozen=True)
class TableProfile:
    table: str
    total_rows: int
    fields: list[FieldProfile] = field(default_factory=list)


def build_table_profile(
    contract: Contract,
    type_registry: TypeRegistry,
    *,
    total_rows: int,
    type_format: str,
) -> TableProfile:
    """Build a `TableProfile` of per-field metadata for one table.

    Pure Python -- no Polars touch -- because the metrics registry now
    owns every numeric per-field statistic. Each contract field gets a
    `FieldProfile` carrying its target-resolved physical type plus the
    pk/fk flags.

    `type_format` is the active target's name (e.g. `"oracle"`); it
    flows into each FieldProfile so the report can show which target's
    type syntax `type` reflects.
    """
    pk_names = {f.name for f in contract.primary_key_fields()}
    fk_names = {f.name for f in contract.foreign_key_fields()}

    profiles: list[FieldProfile] = []
    for fc in contract.fields:
        try:
            physical = type_registry.physical_type_for(fc) or fc.type.value
        except Exception:
            physical = fc.type.value
        profiles.append(FieldProfile(
            name=fc.name,
            type=physical,
            type_format=type_format,
            is_pk=fc.name in pk_names,
            is_fk=fc.name in fk_names,
            total=total_rows,
        ))

    return TableProfile(table=contract.table, total_rows=total_rows, fields=profiles)
