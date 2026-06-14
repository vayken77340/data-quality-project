"""Plain-English action hints per violation kind.

Each entry is a single sentence telling the spec author / data steward what
to do about the violation. No regex syntax, no jargon, no code references --
the hint should make sense to a non-engineer reading the business report.

Adding a new violation kind requires adding both a dimension (in
`dimensions._KIND_TO_DIMENSION`) AND a hint here -- the test suite enforces
both.
"""

from __future__ import annotations


HINTS: dict[str, str] = {
    # Completeness
    "nullable_violation": (
        "This field is required but a row left it empty. "
        "Check the upstream extract for missing values, or relax the contract's nullable setting if blanks are acceptable."
    ),
    "column_missing": (
        "The contract expects this column but the data file does not contain it. "
        "Add the column upstream, rename a misnamed one, or remove the field from the contract."
    ),
    # Validity
    "type_coercion_violation": (
        "The value does not match the field's declared type. "
        "Check the source data for typos, locale-specific formatting (e.g. French decimal commas), or update the contract type."
    ),
    "max_length_violation": (
        "The value is longer than the field's max_length. "
        "Either shorten the value upstream or raise the cap. For target databases that count bytes (Oracle), remember multi-byte characters take more than one byte."
    ),
    "boolean_coercion_violation": (
        "The value is not in the accepted boolean token list. "
        "Use the documented tokens (e.g. true/false, yes/no, Y/N) or add the token to the type registry's data_values block."
    ),
    "pattern_violation": (
        "The value does not match the field's pattern constraint. "
        "Fix the source data or update the pattern in the contract."
    ),
    "format_violation": (
        "The value does not match the field's declared format (e.g. email, uuid). "
        "Fix the source data or remove the format declaration if it no longer applies."
    ),
    "allowed_values_violation": (
        "The value is not in the field's allowed_values list. "
        "Add the value to the list if it is now valid, or fix the source."
    ),
    "min_value_violation": (
        "The value is below the field's minimum. "
        "Check the source for outliers or extraction errors, or relax the minimum if the value is legitimate."
    ),
    "max_value_violation": (
        "The value is above the field's maximum. "
        "Check the source for outliers or extraction errors, or relax the maximum if the value is legitimate."
    ),
    "extra_column": (
        "The data file has a column that the contract does not declare. "
        "Add it to the contract if it is now part of the schema, or drop it upstream."
    ),
    # Uniqueness
    "pk_not_unique": (
        "The primary key is duplicated across multiple rows. "
        "Deduplicate the source data on this key, or change the PK declaration if duplicates are expected."
    ),
    "unique_violation": (
        "The field is declared unique but a value appears more than once. "
        "Deduplicate the source data, or remove the unique constraint."
    ),
    # Consistency
    "fk_not_found": (
        "The foreign-key value does not exist in the parent table. "
        "Check the parent's data for missing rows, fix the FK value, or relax the foreign-key constraint."
    ),
    # Operational
    "no_input_files": (
        "No data files matched the file_pattern. "
        "Check the input directory and the file_pattern in validation.yaml."
    ),
    "parser_failure": (
        "The parser could not read the file. "
        "Check the file format, encoding, or parser_overrides settings."
    ),
    "fk_target_table_not_loaded": (
        "The foreign-key target table was not validated in this run. "
        "Re-run without a --table filter or include the target table in the validation.yaml tables block."
    ),
    # Source-schema drift (parser declared a schema that diverges from the contract).
    "field_name_drift": (
        "A column name declared by the source file does not match the contract (or vice versa). "
        "Confirm whether the upstream team renamed a column -- update the contract field or correct the source extract."
    ),
    "field_type_drift": (
        "A column's type declared by the source file disagrees with the contract type after normalisation. "
        "Either update the contract type to match what upstream now produces, or fix the source-side type if the contract is authoritative."
    ),
    "field_type_unknown": (
        "The source declared a type the parser doesn't know how to translate to a contract Type. "
        "Extend the parser's SOURCE_TYPE_ALIASES with the new source-side type so the drift check can compare it next run."
    ),
}


def hint_for(kind: str) -> str:
    """Return the action hint for a violation kind. Raises KeyError on unknown."""
    try:
        return HINTS[kind]
    except KeyError:
        raise KeyError(
            f"unknown violation kind {kind!r}; add a one-sentence hint to "
            f"validation.report.hints.HINTS"
        ) from None
