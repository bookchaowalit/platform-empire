-- SPEC-006 review fix (2026-11-13): sign-up stays idempotent after IDENTITY_INVITE_KEY_PREVIOUS is dropped.
-- signups.subject_hmac changes with the invite key, so a person who signs up again after the rotation window
-- would get a second workspace. Each sign-up now also records the identity (a stable id, keyed by the Google
-- subject in platform.identities), and tenancy_signup matches on it as well as on the HMACs.

ALTER TABLE platform.signups ADD COLUMN identity_id uuid REFERENCES platform.identities (id);
-- Rows from 0009: the workspace owner at sign-up is the signer (ownership cannot move yet).
UPDATE platform.signups s SET identity_id = w.owner_identity_id
FROM platform.workspaces w WHERE w.id = s.workspace_id AND s.identity_id IS NULL;
CREATE UNIQUE INDEX signups_identity_product ON platform.signups (identity_id, product) WHERE identity_id IS NOT NULL;

-- The function belongs to platform_tenancy (0009); only its owner may replace it. Same hand-over as 0009:
-- CREATE on the schema for this statement only. Owner, ACL and EXECUTE grants are kept by CREATE OR REPLACE.
GRANT CREATE ON SCHEMA platform TO platform_tenancy;
SET ROLE platform_tenancy;
CREATE OR REPLACE FUNCTION platform.tenancy_signup(
    p_subject text, p_subject_hmacs text[], p_display_name text, p_product text, p_slug text, p_name text,
    p_max_owned integer, p_roles jsonb, p_installs jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_identity uuid;
    v_status   text;
    v_ws       uuid;
    v_slug     text;
    v_role     uuid;
    v_owner    uuid;
    v_name     text;
    v_perms    jsonb;
    v_client   jsonb;
BEGIN
    IF p_roles IS NULL OR NOT p_roles ? 'owner' THEN
        RAISE EXCEPTION 'invalid_roles' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- p_subject_hmacs: the HMAC under the current key first, then the previous key during a rotation.
    IF cardinality(p_subject_hmacs) IS NULL OR cardinality(p_subject_hmacs) NOT BETWEEN 1 AND 2 THEN
        RAISE EXCEPTION 'invalid_request' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('tenancy-signup:' || p_subject, 0));
    SELECT id, status INTO v_identity, v_status FROM platform.identities WHERE external_subject = p_subject;
    -- The identity is the lasting key; the HMACs still find rows made before 0010 and during a rotation.
    SELECT s.workspace_id, w.slug INTO v_ws, v_slug
    FROM platform.signups s JOIN platform.workspaces w ON w.id = s.workspace_id
    WHERE s.product = p_product
      AND (s.subject_hmac = ANY (p_subject_hmacs) OR (v_identity IS NOT NULL AND s.identity_id = v_identity))
    LIMIT 1;
    IF v_status = 'disabled' THEN
        RAISE EXCEPTION 'invalid_grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_ws IS NOT NULL THEN
        RETURN jsonb_build_object('workspace_id', v_ws, 'slug', v_slug, 'identity_id', v_identity, 'created', false);
    END IF;
    IF v_identity IS NULL THEN
        INSERT INTO platform.identities (kind, display_name, external_subject)
        VALUES ('human', left(coalesce(nullif(p_display_name, ''), 'Member'), 200), p_subject)
        RETURNING id INTO v_identity;
    ELSIF (SELECT count(*) FROM platform.workspaces
           WHERE owner_identity_id = v_identity AND status <> 'closed') >= p_max_owned THEN
        RAISE EXCEPTION 'workspace_limit' USING ERRCODE = 'check_violation';
    END IF;
    v_ws := platform.uuid_v7();
    PERFORM platform.tenancy_bind(v_ws);
    BEGIN
        INSERT INTO platform.workspaces (id, slug, name, owner_identity_id, product)
        VALUES (v_ws, p_slug, left(p_name, 200), v_identity, p_product);
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'slug_taken' USING ERRCODE = 'unique_violation';
    END;
    INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (v_ws, v_identity);
    FOR v_name, v_perms IN SELECT key, value FROM jsonb_each(p_roles) LOOP
        IF v_name LIKE 'installation:%' THEN
            RAISE EXCEPTION 'invalid_roles' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        INSERT INTO platform.roles (workspace_id, name) VALUES (v_ws, v_name) RETURNING id INTO v_role;
        INSERT INTO platform.role_permissions (workspace_id, role_id, permission)
        SELECT v_ws, v_role, p::platform.permission FROM jsonb_array_elements_text(v_perms) AS p;
        IF v_name = 'owner' THEN
            v_owner := v_role;
        END IF;
    END LOOP;
    INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (v_ws, v_identity, v_owner);
    FOR v_client IN SELECT value FROM jsonb_array_elements(coalesce(p_installs, '[]'::jsonb)) LOOP
        PERFORM platform.tenancy_add_installation(v_ws, v_client);
    END LOOP;
    INSERT INTO platform.signups (subject_hmac, product, workspace_id, identity_id)
    VALUES (p_subject_hmacs[1], p_product, v_ws, v_identity);
    PERFORM platform.tenancy_audit(v_identity, 'workspace.created', 'workspace', v_ws::text);
    RETURN jsonb_build_object('workspace_id', v_ws, 'slug', p_slug, 'identity_id', v_identity, 'created', true);
END
$$;

RESET ROLE;
REVOKE CREATE ON SCHEMA platform FROM platform_tenancy;
