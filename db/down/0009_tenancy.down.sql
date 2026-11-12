-- Manual rollback of services/platform/migrations/0009_tenancy.sql (SPEC-006). Not applied by `migrate`.
-- Run as empire_migrator after a Neon snapshot branch; then delete the 0009 row from public.schema_migrations.
-- It drops tenant sign-up data (signups, invitations); workspaces, memberships and roles made by sign-up stay.
BEGIN;
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'platform' AND p.proname LIKE 'tenancy\_%'
    LOOP
        EXECUTE format('DROP FUNCTION %s', f);
    END LOOP;
END
$$;
DROP POLICY IF EXISTS tenancy_read ON platform.identities;
DROP POLICY IF EXISTS tenancy_read ON platform.workspaces;
DROP POLICY IF EXISTS tenancy_read ON platform.memberships;
DROP POLICY IF EXISTS tenancy_read ON platform.roles;
DROP POLICY IF EXISTS tenancy_read ON platform.membership_roles;
DROP TABLE platform.invitations;
DROP TABLE platform.signups;
ALTER TABLE platform.workspaces DROP COLUMN product;
REVOKE ALL ON ALL TABLES IN SCHEMA platform FROM platform_tenancy;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA platform FROM platform_tenancy;
REVOKE USAGE ON SCHEMA platform FROM platform_tenancy;
COMMIT;
