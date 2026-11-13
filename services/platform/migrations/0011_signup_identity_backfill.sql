-- SPEC-006 review fix (2026-11-13): 0010's backfill of signups.identity_id ran as empire_migrator under
-- FORCE ROW LEVEL SECURITY on platform.signups and platform.workspaces, with no app.workspace_id set, so it saw
-- no rows and filled nothing. Rows made by 0009 kept identity_id NULL and stayed findable only by HMAC.
--
-- The migrator owns both tables, so with FORCE lifted inside this migration's transaction the owner bypasses
-- RLS for the one statement; FORCE is restored before the transaction ends (migrate runs each file in one
-- transaction, so no other session ever sees the tables unforced).
ALTER TABLE platform.signups NO FORCE ROW LEVEL SECURITY;
ALTER TABLE platform.workspaces NO FORCE ROW LEVEL SECURITY;
UPDATE platform.signups s SET identity_id = w.owner_identity_id
FROM platform.workspaces w WHERE w.id = s.workspace_id AND s.identity_id IS NULL;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM platform.signups WHERE identity_id IS NULL) THEN
        RAISE EXCEPTION 'signups without identity_id remain after the backfill';
    END IF;
END
$$;
ALTER TABLE platform.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE platform.signups FORCE ROW LEVEL SECURITY;
