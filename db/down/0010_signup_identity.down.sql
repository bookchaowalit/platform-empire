-- Manual rollback of services/platform/migrations/0010_signup_identity.sql. Not applied by `migrate`.
-- Run as empire_migrator; then re-run the tenancy_signup definition from 0009 (same SET ROLE hand-over)
-- and delete the 0010 row from public.schema_migrations. Dropping the column loses only the identity link.
BEGIN;
DROP INDEX IF EXISTS platform.signups_identity_product;
ALTER TABLE platform.signups DROP COLUMN identity_id;
COMMIT;
