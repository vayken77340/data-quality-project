"""Per-field data profile.

Computes basic descriptive stats per contract field from the eager Polars
DataFrame the runner already collected. Stats are surfaced alongside the
violation list so reports answer both "what's broken?" and "what does the
data look like?" -- the second question that gold-standard data quality tools
always answer alongside rule violations.

The profile is intentionally cheap: a single Polars pass per column. No
per-value Python iteration; no second DataFrame collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data_contract.contract import Contract, FieldContract
from data_contract.type_mapping import Type, TypeRegistry


@dataclass(frozen=True)
class FieldProfile:
    """Per-field profile shown in the report's profile sheet.

    Carries the columns:
        Field, Type, Type Format, Primary Key, Foreign Key,
        Total, Null #, Null %, Distinct
    """
    name: str
    type: str                         # target's physical_type, e.g. "VARCHAR2(384 BYTE)"
    type_format: str                  # active target's name, e.g. "oracle"
    is_pk: bool                       # rendered as "Primary Key"
    is_fk: bool                       # rendered as "Foreign Key"
    total: int                        # total rows in the table
    null_count: int
    null_pct: float                   # 0.0 - 100.0
    distinct_count: int


@dataclass(frozen=True)
class TableProfile:
    table: str
    total_rows: int
    fields: list[FieldProfile] = field(default_factory=list)


def build_table_profile(
    df,
    contract: Contract,
    type_registry: TypeRegistry,
    type_format: str,
) -> TableProfile:
    """Compute a `TableProfile` from the eager Polars DataFrame.

    `df` is the post-normalisation DataFrame the runner uses for per-constraint
    checks. Each contract field present in the frame gets a `FieldProfile`;
    fields absent from the frame (e.g. column_missing case) get a placeholder
    entry with `null_count == total`.

    `type_format` is the active target's name (e.g. `"oracle"`) -- carried per
    field so the profile sheet can show which target's type syntax `type`
    reflects.
    """
    total = df.height
    pk_names = {f.name for f in contract.primary_key_fields()}
    fk_names = {f.name for f in contract.foreign_key_fields()}

    profiles: list[FieldProfile] = []
    for fc in contract.fields:
        try:
            physical = type_registry.physical_type_for(fc) or fc.type.value
        except Exception:
            physical = fc.type.value

        if fc.name not in df.columns:
            profiles.append(FieldProfile(
                name=fc.name,
                type=physical,
                type_format=type_format,
                is_pk=fc.name in pk_names,
                is_fk=fc.name in fk_names,
                total=total,
                null_count=total,
                null_pct=100.0 if total else 0.0,
                distinct_count=0,
            ))
            continue

        profiles.append(_profile_one_field(
            df=df,
            fc=fc,
            total=total,
            physical=physical,
            type_format=type_format,
            is_pk=fc.name in pk_names,
            is_fk=fc.name in fk_names,
        ))

    return TableProfile(table=contract.table, total_rows=total, fields=profiles)


def _profile_one_field(
    *,
    df,
    fc: FieldContract,
    total: int,
    physical: str,
    type_format: str,
    is_pk: bool,
    is_fk: bool,
) -> FieldProfile:
    col = df[fc.name]
    null_count = int(col.is_null().sum())
    null_pct = round((null_count / total) * 100, 2) if total else 0.0
    distinct_count = int(col.n_unique())
    # n_unique() in Polars counts null as a distinct value; subtract one when
    # there are actual nulls so distinct_count means "distinct non-null values".
    if null_count > 0 and distinct_count > 0:
        distinct_count -= 1

    return FieldProfile(
        name=fc.name,
        type=physical,
        type_format=type_format,
        is_pk=is_pk,
        is_fk=is_fk,
        total=total,
        null_count=null_count,
        null_pct=null_pct,
        distinct_count=distinct_count,
    )
