-- DBA-only setup example: replace both schema references if your schema differs.
-- Run while connected to the exact HA schema with a reviewed DEFINER identity.
-- Grant the HA account EXECUTE only on this procedure, not global PROCESS.
-- If this procedure already exists, compare its definition instead of replacing blindly.
CREATE PROCEDURE recorder_storage_probe()
SQL SECURITY DEFINER
READS SQL DATA
SELECT 'homeassistant' AS schema_name,
       CAST(COALESCE(SUM(ALLOCATED_SIZE), 0) AS UNSIGNED) AS allocated_bytes,
       CAST(COALESCE(SUM(FILE_SIZE), 0) AS UNSIGNED) AS file_bytes
FROM information_schema.INNODB_TABLESPACES
WHERE SUBSTRING_INDEX(NAME, '/', 1) = 'homeassistant'
  AND SPACE_TYPE = 'Single';
