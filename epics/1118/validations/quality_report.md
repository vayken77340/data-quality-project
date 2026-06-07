# Data quality report - epic 1118 - 2026-06-06T19:26:51Z

Overall: **FAIL** (10 errors, 0 warnings)

## Tables

### PROJECT - 0.00% clean (5 violations / 5 rows)
- 2 `pk_not_unique` errors
- 1 `nullable_violation` errors on `proj_id`
- 1 `nullable_violation` errors on `column1`
- 1 `max_length_violation` errors on `column3`

### CALENDAR - 0.00% clean (5 violations / 5 rows)
- 2 `pk_not_unique` errors
- 1 `nullable_violation` errors on `test_id`
- 1 `nullable_violation` errors on `proj_id`
- 1 `fk_not_found` errors on `proj_id`
