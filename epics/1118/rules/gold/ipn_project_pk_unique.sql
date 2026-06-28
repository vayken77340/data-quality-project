-- Returns the number of duplicate composite (proj_id, column1) PK rows
-- on the silver ipn_project table. Zero means the contract's declared
-- primary key is unique end-to-end. Tally is COUNT(*) minus the count
-- of distinct PK combinations.
SELECT COUNT(*) - COUNT(DISTINCT CAST(proj_id AS VARCHAR) || '|' || column1)
FROM placeholder_catalog.placeholder_schema.ipn_project_silver
