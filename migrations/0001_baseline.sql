-- folder_actions — database-wide baseline. Run ONCE per database:
--   psql "host=$FA_PG_HOST port=$FA_PG_PORT dbname=$FA_PG_DATABASE user=$FA_PG_USER" \
--        -f migrations/0001_baseline.sql
--
-- Per-tenant tables are provisioned in code (schema.ensure_tenant_schema); this file
-- holds only database-wide objects. folder_actions needs no extensions —
-- gen_random_uuid() is native in PostgreSQL 13+. This file is intentionally a no-op
-- placeholder so the provisioning convention (and future DB-wide objects) has a home.
SELECT 1;
