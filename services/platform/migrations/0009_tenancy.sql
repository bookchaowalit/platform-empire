-- Tenancy: sign-up, invitations, members, workspace status and service installations
-- (platform-identity SPEC-006, approved by the owner 2026-11-12).
--
-- identity_api still gets no table write grant. It calls the SECURITY DEFINER
-- functions below, which are owned by platform_tenancy: a NOLOGIN role without
-- BYPASSRLS, created before this migration (db/bootstrap/roles.sql locally,
-- `empire-neon provision` hosted) and granted to empire_migrator WITH SET so
-- the migrator can hand the functions over (ALTER FUNCTION ... OWNER TO).
--
-- Workspace writes stay under the forced RLS policies: each function sets
-- app.workspace_id itself before it touches a workspace's rows. The only
-- cross-workspace access is a set of SELECT-only policies for platform_tenancy
-- (find an identity by subject, the caller's own memberships, an invitation by
-- its code HMAC, the workspaces an identity owns). Every caller identity and
-- workspace argument is filled by identity from a verified token, never from a
-- request body.
--
-- Errors are raised with a short code as the message (slug_taken, forbidden,
-- not_found, invalid_grant, ...); identity maps them to HTTP answers.

ALTER TABLE platform.workspaces ADD COLUMN product text
    CHECK (product ~ '^[a-z][a-z0-9-]{1,31}$');

CREATE TABLE platform.signups (
    subject_hmac text NOT NULL CHECK (length(subject_hmac) BETWEEN 8 AND 200),
    product      text NOT NULL,
    workspace_id uuid NOT NULL REFERENCES platform.workspaces (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_hmac, product)
);

CREATE TABLE platform.invitations (
    id           uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id uuid NOT NULL REFERENCES platform.workspaces (id),
    email_hmac   text NOT NULL CHECK (length(email_hmac) BETWEEN 8 AND 200),
    email_hint   text NOT NULL CHECK (length(email_hint) <= 16),
    code_hmac    text NOT NULL UNIQUE CHECK (length(code_hmac) BETWEEN 8 AND 200),
    role_id      uuid NOT NULL,
    status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'revoked')),
    expires_at   timestamptz NOT NULL,
    created_by   uuid NOT NULL REFERENCES platform.identities (id),
    accepted_by  uuid REFERENCES platform.identities (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, role_id) REFERENCES platform.roles (workspace_id, id)
);
CREATE INDEX ON platform.invitations (workspace_id, status);
CREATE INDEX ON platform.workspaces (owner_identity_id);

ALTER TABLE platform.signups ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.signups FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON platform.signups
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());
ALTER TABLE platform.invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON platform.invitations
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());

-- Cross-workspace reads for platform_tenancy only (permissive, SELECT only).
CREATE POLICY tenancy_read ON platform.identities FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.workspaces FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.memberships FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.roles FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.membership_roles FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.signups FOR SELECT TO platform_tenancy USING (true);
CREATE POLICY tenancy_read ON platform.invitations FOR SELECT TO platform_tenancy USING (true);

GRANT USAGE ON SCHEMA platform TO platform_tenancy;
GRANT SELECT, INSERT, UPDATE ON platform.identities, platform.workspaces, platform.invitations TO platform_tenancy;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.memberships, platform.membership_roles TO platform_tenancy;
GRANT SELECT, INSERT ON platform.roles, platform.role_permissions, platform.signups TO platform_tenancy;
GRANT SELECT, INSERT, UPDATE ON platform.executions TO platform_tenancy;
GRANT SELECT, INSERT ON platform.audit_events TO platform_tenancy;
GRANT EXECUTE ON FUNCTION platform.uuid_v7(), platform.current_workspace(),
    platform.direct_permission(uuid, text) TO platform_tenancy;

-- --- helpers (not callable by identity_api) -------------------------------------------------------

CREATE FUNCTION platform.tenancy_bind(p_workspace uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT set_config('app.workspace_id', p_workspace::text, true)
$$;

CREATE FUNCTION platform.tenancy_audit(p_actor uuid, p_action text, p_type text, p_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_execution uuid;
BEGIN
    INSERT INTO platform.executions (workspace_id, initiated_by, executed_by, on_behalf_of, channel,
                                     status, finished_at)
    VALUES (platform.current_workspace(), p_actor, p_actor, p_actor, 'api', 'succeeded', now())
    RETURNING id INTO v_execution;
    INSERT INTO platform.audit_events (workspace_id, execution_id, service, action, resource_type, resource_id)
    VALUES (platform.current_workspace(), v_execution, 'identity', p_action, p_type, p_id);
END
$$;

-- Active human member of an active workspace holding the permission; raises otherwise.
CREATE FUNCTION platform.tenancy_require(p_caller uuid, p_workspace uuid, p_permission text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_bind(p_workspace);
    IF NOT platform.direct_permission(p_caller, p_permission) THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$$;

CREATE FUNCTION platform.tenancy_is_owner(p_identity uuid, p_workspace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM platform.membership_roles mr
        JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
        JOIN platform.memberships m ON m.workspace_id = mr.workspace_id AND m.identity_id = mr.identity_id
        JOIN platform.identities i ON i.id = mr.identity_id
        WHERE mr.workspace_id = p_workspace AND mr.identity_id = p_identity AND r.name = 'owner'
          AND m.status = 'active' AND i.status = 'active' AND i.kind = 'human')
$$;

-- Holds the owner role whatever the membership status: a suspended owner is still an owner to protect.
CREATE FUNCTION platform.tenancy_holds_owner(p_identity uuid, p_workspace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM platform.membership_roles mr
        JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
        WHERE mr.workspace_id = p_workspace AND mr.identity_id = p_identity AND r.name = 'owner')
$$;

-- A non-owner may only hand out roles whose every permission it holds itself.
CREATE FUNCTION platform.tenancy_require_role_permissions(p_caller uuid, p_workspace uuid, p_roles text[])
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF platform.tenancy_is_owner(p_caller, p_workspace) THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM platform.roles r
        JOIN platform.role_permissions rp ON rp.workspace_id = r.workspace_id AND rp.role_id = r.id
        WHERE r.workspace_id = p_workspace AND r.name = ANY (p_roles)
          AND NOT platform.direct_permission(p_caller, rp.permission)) THEN
        RAISE EXCEPTION 'insufficient_privilege' USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$$;

-- The same rule for a member a non-owner manages: it must hold every permission of the member's current roles.
CREATE FUNCTION platform.tenancy_require_member_permissions(p_caller uuid, p_workspace uuid, p_member uuid)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_require_role_permissions(p_caller, p_workspace, ARRAY(
        SELECT r.name FROM platform.membership_roles mr
        JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
        WHERE mr.workspace_id = p_workspace AND mr.identity_id = p_member));
END
$$;

-- Lock the owner rows, then refuse if no active human owner would remain.
CREATE FUNCTION platform.tenancy_check_owners(p_workspace uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM platform.membership_roles mr
        JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
        JOIN platform.memberships m ON m.workspace_id = mr.workspace_id AND m.identity_id = mr.identity_id
        JOIN platform.identities i ON i.id = mr.identity_id
        WHERE mr.workspace_id = p_workspace AND r.name = 'owner'
          AND m.status = 'active' AND i.status = 'active' AND i.kind = 'human') THEN
        RAISE EXCEPTION 'last_owner' USING ERRCODE = 'check_violation';
    END IF;
END
$$;

CREATE FUNCTION platform.tenancy_lock_owners(p_workspace uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    -- One lock per workspace serializes every owner change in it.
    SELECT pg_advisory_xact_lock(hashtextextended('tenancy-owners:' || p_workspace::text, 0))
$$;

CREATE FUNCTION platform.tenancy_add_installation(p_workspace uuid, p_client jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_subject uuid := (p_client ->> 'subject')::uuid;
    v_client  text := p_client ->> 'client_id';
    v_kind    text;
    v_role    uuid;
BEGIN
    IF v_client IS NULL OR v_client !~ '^[a-z][a-z0-9-]{2,62}$' OR v_subject IS NULL THEN
        RAISE EXCEPTION 'invalid_client' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT kind INTO v_kind FROM platform.identities WHERE id = v_subject;
    IF v_kind IS NULL THEN
        INSERT INTO platform.identities (id, kind, display_name)
        VALUES (v_subject, 'service_account', coalesce(p_client ->> 'display_name', v_client));
    ELSIF v_kind <> 'service_account' THEN
        RAISE EXCEPTION 'invalid_client' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF EXISTS (SELECT 1 FROM platform.memberships WHERE workspace_id = p_workspace AND identity_id = v_subject) THEN
        RETURN false;
    END IF;
    INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (p_workspace, v_subject);
    SELECT id INTO v_role FROM platform.roles WHERE workspace_id = p_workspace AND name = 'installation:' || v_client;
    IF v_role IS NULL THEN
        INSERT INTO platform.roles (workspace_id, name) VALUES (p_workspace, 'installation:' || v_client)
        RETURNING id INTO v_role;
        INSERT INTO platform.role_permissions (workspace_id, role_id, permission)
        SELECT p_workspace, v_role, p::platform.permission
        FROM jsonb_array_elements_text(coalesce(p_client -> 'permissions', '[]'::jsonb)) AS p;
    END IF;
    INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (p_workspace, v_subject, v_role);
    RETURN true;
END
$$;

-- --- entry points for identity_api ----------------------------------------------------------------

CREATE FUNCTION platform.tenancy_signup(
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
    SELECT s.workspace_id, w.slug INTO v_ws, v_slug
    FROM platform.signups s JOIN platform.workspaces w ON w.id = s.workspace_id
    WHERE s.subject_hmac = ANY (p_subject_hmacs) AND s.product = p_product
    LIMIT 1;
    SELECT id, status INTO v_identity, v_status FROM platform.identities WHERE external_subject = p_subject;
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
    INSERT INTO platform.signups (subject_hmac, product, workspace_id) VALUES (p_subject_hmacs[1], p_product, v_ws);
    PERFORM platform.tenancy_audit(v_identity, 'workspace.created', 'workspace', v_ws::text);
    RETURN jsonb_build_object('workspace_id', v_ws, 'slug', p_slug, 'identity_id', v_identity, 'created', true);
END
$$;

CREATE FUNCTION platform.tenancy_my_workspaces(p_subject text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'workspace_id', w.id, 'slug', w.slug, 'name', w.name, 'product', w.product, 'status', w.status,
               'roles', coalesce((SELECT jsonb_agg(r.name ORDER BY r.name)
                                  FROM platform.membership_roles mr
                                  JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
                                  WHERE mr.workspace_id = w.id AND mr.identity_id = i.id), '[]'::jsonb))
               ORDER BY w.created_at), '[]'::jsonb)
    FROM platform.identities i
    JOIN platform.memberships m ON m.identity_id = i.id AND m.status = 'active'
    JOIN platform.workspaces w ON w.id = m.workspace_id AND w.status IN ('active', 'suspended')
    WHERE i.external_subject = p_subject AND i.status = 'active'
$$;

CREATE FUNCTION platform.tenancy_invite(
    p_caller uuid, p_workspace uuid, p_email_hmac text, p_email_hint text, p_code_hmac text, p_role text,
    p_ttl_seconds integer
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_role uuid;
    v_id   uuid;
    v_exp  timestamptz := now() + make_interval(secs => p_ttl_seconds);
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    IF p_role = 'owner' OR p_role LIKE 'installation:%' THEN
        RAISE EXCEPTION 'invalid_role' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT id INTO v_role FROM platform.roles WHERE workspace_id = p_workspace AND name = p_role;
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'invalid_role' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM platform.tenancy_require_role_permissions(p_caller, p_workspace, ARRAY[p_role]);
    INSERT INTO platform.invitations (workspace_id, email_hmac, email_hint, code_hmac, role_id, expires_at, created_by)
    VALUES (p_workspace, p_email_hmac, p_email_hint, p_code_hmac, v_role, v_exp, p_caller)
    RETURNING id INTO v_id;
    PERFORM platform.tenancy_audit(p_caller, 'member.invited', 'invitation', v_id::text);
    RETURN jsonb_build_object('invitation_id', v_id, 'expires_at', v_exp);
END
$$;

CREATE FUNCTION platform.tenancy_list_invitations(p_caller uuid, p_workspace uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    RETURN (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'invitation_id', i.id, 'role', r.name, 'email_hint', i.email_hint, 'status', i.status,
                'expires_at', i.expires_at) ORDER BY i.created_at), '[]'::jsonb)
            FROM platform.invitations i JOIN platform.roles r ON r.workspace_id = i.workspace_id AND r.id = i.role_id
            WHERE i.workspace_id = p_workspace AND i.status = 'open' AND i.expires_at > now());
END
$$;

CREATE FUNCTION platform.tenancy_revoke_invitation(p_caller uuid, p_workspace uuid, p_invitation uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    UPDATE platform.invitations SET status = 'revoked'
    WHERE id = p_invitation AND workspace_id = p_workspace AND status = 'open';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not_found' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM platform.tenancy_audit(p_caller, 'member.invitation_revoked', 'invitation', p_invitation::text);
END
$$;

-- p_code_hmacs / p_email_hmacs hold the HMAC under the current key and, during a key rotation, the previous one.
CREATE FUNCTION platform.tenancy_accept(
    p_subject text, p_display_name text, p_code_hmacs text[], p_email_hmacs text[]
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_inv      uuid;
    v_ws       uuid;
    v_role     uuid;
    v_identity uuid;
    v_status   text;
BEGIN
    SELECT i.id, i.workspace_id INTO v_inv, v_ws
    FROM platform.invitations i JOIN platform.workspaces w ON w.id = i.workspace_id
    WHERE i.code_hmac = ANY (p_code_hmacs) AND i.email_hmac = ANY (p_email_hmacs);
    IF v_ws IS NULL THEN
        RAISE EXCEPTION 'invalid_grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM platform.tenancy_bind(v_ws);
    -- Single use: only one concurrent accept can move the row out of 'open'.
    UPDATE platform.invitations i SET status = 'accepted'
    FROM platform.workspaces w
    WHERE i.id = v_inv AND w.id = i.workspace_id AND i.status = 'open' AND i.expires_at > now()
      AND w.status = 'active'
    RETURNING i.role_id INTO v_role;
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'invalid_grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT id, status INTO v_identity, v_status FROM platform.identities WHERE external_subject = p_subject;
    IF v_status = 'disabled' THEN
        RAISE EXCEPTION 'invalid_grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_identity IS NULL THEN
        INSERT INTO platform.identities (kind, display_name, external_subject)
        VALUES ('human', left(coalesce(nullif(p_display_name, ''), 'Member'), 200), p_subject)
        RETURNING id INTO v_identity;
    END IF;
    UPDATE platform.invitations SET accepted_by = v_identity WHERE id = v_inv;
    IF NOT EXISTS (SELECT 1 FROM platform.memberships WHERE workspace_id = v_ws AND identity_id = v_identity) THEN
        INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (v_ws, v_identity);
        INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (v_ws, v_identity, v_role);
        PERFORM platform.tenancy_audit(v_identity, 'member.joined', 'identity', v_identity::text);
    END IF;
    RETURN jsonb_build_object('workspace_id', v_ws);
END
$$;

CREATE FUNCTION platform.tenancy_list_members(p_caller uuid, p_workspace uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:read');
    RETURN (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'identity_id', i.id, 'kind', i.kind, 'display_name', i.display_name, 'status', m.status,
                'roles', coalesce((SELECT jsonb_agg(r.name ORDER BY r.name) FROM platform.membership_roles mr
                                   JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
                                   WHERE mr.workspace_id = m.workspace_id AND mr.identity_id = i.id), '[]'::jsonb))
                ORDER BY m.created_at), '[]'::jsonb)
            FROM platform.memberships m JOIN platform.identities i ON i.id = m.identity_id
            WHERE m.workspace_id = p_workspace);
END
$$;

CREATE FUNCTION platform.tenancy_set_member(
    p_caller uuid, p_workspace uuid, p_member uuid, p_roles text[], p_status text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_kind      text;
    v_was_owner boolean;
    v_unknown   integer;
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    PERFORM platform.tenancy_lock_owners(p_workspace);
    SELECT i.kind INTO v_kind FROM platform.memberships m JOIN platform.identities i ON i.id = m.identity_id
    WHERE m.workspace_id = p_workspace AND m.identity_id = p_member;
    IF v_kind IS NULL THEN
        RAISE EXCEPTION 'not_found' USING ERRCODE = 'no_data_found';
    ELSIF v_kind = 'service_account' THEN
        RAISE EXCEPTION 'installation_fixed' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_status IS NOT NULL AND p_status NOT IN ('active', 'suspended') THEN
        RAISE EXCEPTION 'invalid_status' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_was_owner := platform.tenancy_holds_owner(p_member, p_workspace);
    -- Any change to a member (roles or status) needs the caller to hold what the member holds now.
    PERFORM platform.tenancy_require_member_permissions(p_caller, p_workspace, p_member);
    PERFORM platform.tenancy_audit(p_caller, 'member.updated', 'identity', p_member::text);
    IF p_roles IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM unnest(p_roles) r WHERE r LIKE 'installation:%') THEN
            RAISE EXCEPTION 'invalid_role' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        SELECT count(*) INTO v_unknown FROM unnest(p_roles) r
        WHERE NOT EXISTS (SELECT 1 FROM platform.roles WHERE workspace_id = p_workspace AND name = r);
        IF v_unknown > 0 THEN
            RAISE EXCEPTION 'invalid_role' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (v_was_owner OR 'owner' = ANY (p_roles)) AND NOT platform.tenancy_is_owner(p_caller, p_workspace) THEN
            RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
        END IF;
        PERFORM platform.tenancy_require_role_permissions(p_caller, p_workspace, p_roles);
        DELETE FROM platform.membership_roles WHERE workspace_id = p_workspace AND identity_id = p_member;
        INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id)
        SELECT p_workspace, p_member, r.id FROM platform.roles r
        WHERE r.workspace_id = p_workspace AND r.name = ANY (p_roles);
    END IF;
    IF p_status IS NOT NULL THEN
        IF v_was_owner AND NOT platform.tenancy_is_owner(p_caller, p_workspace) THEN
            RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
        END IF;
        UPDATE platform.memberships SET status = p_status WHERE workspace_id = p_workspace AND identity_id = p_member;
    END IF;
    PERFORM platform.tenancy_check_owners(p_workspace);
END
$$;

CREATE FUNCTION platform.tenancy_remove_member(p_caller uuid, p_workspace uuid, p_member uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    PERFORM platform.tenancy_lock_owners(p_workspace);
    IF NOT EXISTS (SELECT 1 FROM platform.memberships WHERE workspace_id = p_workspace AND identity_id = p_member) THEN
        RAISE EXCEPTION 'not_found' USING ERRCODE = 'no_data_found';
    END IF;
    IF platform.tenancy_holds_owner(p_member, p_workspace) AND NOT platform.tenancy_is_owner(p_caller, p_workspace) THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM platform.tenancy_require_member_permissions(p_caller, p_workspace, p_member);
    -- The execution is recorded first: the caller may be removing itself.
    PERFORM platform.tenancy_audit(p_caller, 'member.removed', 'identity', p_member::text);
    DELETE FROM platform.membership_roles WHERE workspace_id = p_workspace AND identity_id = p_member;
    DELETE FROM platform.memberships WHERE workspace_id = p_workspace AND identity_id = p_member;
    PERFORM platform.tenancy_check_owners(p_workspace);
END
$$;

-- Owner only, checked directly (not through direct_permission, which refuses everything in a suspended workspace).
CREATE FUNCTION platform.tenancy_update_workspace(p_caller uuid, p_workspace uuid, p_name text, p_status text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_status text;
BEGIN
    PERFORM platform.tenancy_bind(p_workspace);
    SELECT status INTO v_status FROM platform.workspaces WHERE id = p_workspace FOR UPDATE;
    IF v_status IS NULL OR NOT platform.tenancy_is_owner(p_caller, p_workspace) THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
    ELSIF v_status = 'closed' THEN
        RAISE EXCEPTION 'workspace_closed' USING ERRCODE = 'check_violation';
    END IF;
    IF p_status IS NOT NULL AND p_status NOT IN ('active', 'suspended', 'closed') THEN
        RAISE EXCEPTION 'invalid_status' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM platform.tenancy_audit(p_caller, 'workspace.updated', 'workspace', p_workspace::text);
    UPDATE platform.workspaces SET name = coalesce(left(p_name, 200), name), status = coalesce(p_status, status)
    WHERE id = p_workspace;
END
$$;

-- Workspaces where this service account is installed (active member of an active workspace), by id.
CREATE FUNCTION platform.tenancy_installations(p_identity uuid, p_after uuid, p_limit integer) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb) FROM (
        SELECT w.id FROM platform.memberships m
        JOIN platform.workspaces w ON w.id = m.workspace_id
        JOIN platform.identities i ON i.id = m.identity_id
        WHERE m.identity_id = p_identity AND m.status = 'active' AND w.status = 'active'
          AND i.kind = 'service_account' AND i.status = 'active'
          AND (p_after IS NULL OR w.id > p_after)
        ORDER BY w.id LIMIT least(greatest(p_limit, 1), 500)) page
$$;

-- The identity's kind when it may act in the workspace now, else NULL. With p_owner_reactivation a suspended
-- workspace counts for its active human owners only (AC-12).
CREATE FUNCTION platform.tenancy_active(p_identity uuid, p_workspace uuid, p_owner_reactivation boolean)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_kind text;
    v_ws   text;
BEGIN
    PERFORM platform.tenancy_bind(p_workspace);
    SELECT i.kind, w.status INTO v_kind, v_ws
    FROM platform.memberships m
    JOIN platform.identities i ON i.id = m.identity_id
    JOIN platform.workspaces w ON w.id = m.workspace_id
    WHERE m.workspace_id = p_workspace AND m.identity_id = p_identity AND m.status = 'active'
      AND i.status = 'active' AND i.kind <> 'agent';
    IF v_ws = 'active' THEN
        RETURN v_kind;
    ELSIF v_ws = 'suspended' AND p_owner_reactivation AND platform.tenancy_is_owner(p_identity, p_workspace) THEN
        RETURN v_kind;
    END IF;
    RETURN NULL;
END
$$;

CREATE FUNCTION platform.tenancy_install(p_caller uuid, p_workspace uuid, p_client jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_missing text;
    v_created boolean;
BEGIN
    PERFORM platform.tenancy_require(p_caller, p_workspace, 'identity.member:admin');
    -- Only clients registered for this workspace's product (identity passes the registry's list).
    IF NOT EXISTS (SELECT 1 FROM platform.workspaces w
                   WHERE w.id = p_workspace AND coalesce(p_client -> 'products', '[]'::jsonb) ? w.product) THEN
        RAISE EXCEPTION 'invalid_client' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- Installing never grants more than the admin holds.
    SELECT p INTO v_missing FROM jsonb_array_elements_text(coalesce(p_client -> 'permissions', '[]'::jsonb)) AS p
    WHERE NOT platform.direct_permission(p_caller, p) LIMIT 1;
    -- A reinstall reuses the existing installation role: the admin must hold its permissions too.
    IF v_missing IS NULL THEN
        SELECT rp.permission INTO v_missing FROM platform.roles r
        JOIN platform.role_permissions rp ON rp.workspace_id = r.workspace_id AND rp.role_id = r.id
        WHERE r.workspace_id = p_workspace AND r.name = 'installation:' || (p_client ->> 'client_id')
          AND NOT platform.direct_permission(p_caller, rp.permission)
        LIMIT 1;
    END IF;
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'insufficient_privilege' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_created := platform.tenancy_add_installation(p_workspace, p_client);
    IF v_created THEN
        PERFORM platform.tenancy_audit(p_caller, 'installation.created', 'identity', p_client ->> 'subject');
    END IF;
    RETURN jsonb_build_object('created', v_created);
END
$$;

-- --- ownership and privileges ---------------------------------------------------------------------

-- Privileges are set while the migrator still owns the functions; ALTER ... OWNER carries the ACL over.
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'platform' AND p.proname LIKE 'tenancy\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    END LOOP;
END
$$;
GRANT EXECUTE ON FUNCTION
    platform.tenancy_signup(text, text[], text, text, text, text, integer, jsonb, jsonb),
    platform.tenancy_my_workspaces(text),
    platform.tenancy_invite(uuid, uuid, text, text, text, text, integer),
    platform.tenancy_list_invitations(uuid, uuid),
    platform.tenancy_revoke_invitation(uuid, uuid, uuid),
    platform.tenancy_accept(text, text, text[], text[]),
    platform.tenancy_list_members(uuid, uuid),
    platform.tenancy_set_member(uuid, uuid, uuid, text[], text),
    platform.tenancy_remove_member(uuid, uuid, uuid),
    platform.tenancy_update_workspace(uuid, uuid, text, text),
    platform.tenancy_installations(uuid, uuid, integer),
    platform.tenancy_active(uuid, uuid, boolean),
    platform.tenancy_install(uuid, uuid, jsonb)
TO identity_api;

-- ALTER ... OWNER TO needs the new owner to hold CREATE on the schema at that moment only.
GRANT CREATE ON SCHEMA platform TO platform_tenancy;
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'platform' AND p.proname LIKE 'tenancy\_%'
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO platform_tenancy', f);
    END LOOP;
END
$$;
REVOKE CREATE ON SCHEMA platform FROM platform_tenancy;
