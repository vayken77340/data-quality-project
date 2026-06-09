# Data quality report - epic 1118 - 2026-06-09T22:35:58Z

**FAIL** - 0.0 / 100   (25 errors, 0 warnings)
Target: oracle - 7 check(s) disabled: allowed_values, fk_existence, format, max_value, min_value, pattern, unique

| Dimension | Score | Violations |
|---|---|---|
| Completeness | 50.0 | 5 |
| Validity | 0.0 | 20 |
| Uniqueness | 100.0 | 0 |
| Consistency | 100.0 | 0 |

## Tables

| Table | Score | Rows | Clean | Errors | Warn |
|---|---|---|---|---|---|
| CALENDAR | 0.0 | 5 | 0 | 10 | 0 |
| PROJECT | 0.0 | 5 | 0 | 15 | 0 |

## Top issues

- **5x `boolean_coercion_violation`** on `columnX1` - The value is not in the accepted boolean token list. Use the documented tokens (e.g. true/false, yes/no, Y/N) or add the token to the type registry's data_values block.
- **5x `type_coercion_violation`** on `columnX2` - The value does not match the field's declared type. Check the source data for typos, locale-specific formatting (e.g. French decimal commas), or update the contract type.
- **5x `boolean_coercion_violation`** on `column1` - The value is not in the accepted boolean token list. Use the documented tokens (e.g. true/false, yes/no, Y/N) or add the token to the type registry's data_values block.
- **5x `type_coercion_violation`** on `column2` - The value does not match the field's declared type. Check the source data for typos, locale-specific formatting (e.g. French decimal commas), or update the contract type.
- **5x `nullable_violation`** on `column1` - This field is required but a row left it empty. Check the upstream extract for missing values, or relax the contract's nullable setting if blanks are acceptable.

- Full XLSX / JSON / HTML in the validations directory.
