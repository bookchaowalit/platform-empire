-- Cluster-level roles. Run once per cluster as a superuser (locally) or create
-- the same roles through the Neon console/API (hosted). No passwords live here;
-- hosted credentials come from the secret manager.
--
-- empire_migrator  owns every schema and table and runs migrations only.
-- platform_api     the platform service at runtime: no superuser, no
--                  BYPASSRLS, owns nothing, and cannot assume empire_migrator.
-- tasks_api        the tasks cell at runtime: same limits, its own schema only,
--                  and only the platform authorization functions.
-- tasks_publisher  reads and marks the tasks outbox across workspaces; nothing else.
-- graph_api        the graph service: its own schema and platform.can only.
-- platform_monitor reads counts, versions, outbox state, and decision codes
--                  across workspaces for the daily check; never task content.
-- identity_api     platform-identity: platform.authorize and read-only
--                  identity and membership columns; no writes.
-- platform_tenancy NOLOGIN owner of the tenancy functions (migration 0009); no
--                  BYPASSRLS. The migrator may SET ROLE to it (to hand the
--                  functions over) but does not inherit its privileges.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'empire_migrator') THEN
        CREATE ROLE empire_migrator LOGIN NOSUPERUSER NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_api') THEN
        CREATE ROLE platform_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tasks_api') THEN
        CREATE ROLE tasks_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tasks_publisher') THEN
        CREATE ROLE tasks_publisher LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'graph_api') THEN
        CREATE ROLE graph_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_monitor') THEN
        CREATE ROLE platform_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'identity_api') THEN
        CREATE ROLE identity_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_tenancy') THEN
        CREATE ROLE platform_tenancy NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
GRANT platform_tenancy TO empire_migrator WITH INHERIT FALSE, SET TRUE;
