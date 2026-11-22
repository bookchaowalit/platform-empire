-- SPEC-002: catalog of shared domains, values and metric definitions (specs/002-catalog-domains-values-metrics).
--
-- The platform owns meaning only: concept and metric definition rows plus their graph nodes. Cells keep
-- their data and measured values. Every entry point is a SECURITY DEFINER function bound to
-- platform.current_workspace(); tasks_api gets EXECUTE on the graph.catalog_* entry points and no table
-- grant. Errors are raised with SQLSTATE 'CT' || <http status> (CT403, CT404, CT409, CT422), the message is
-- the error code and DETAIL is a JSON object, so the HTTP layer maps them without parsing text.
-- A write the decision point refuses is not raised: the entry point returns
-- {"status": 403, "error": <decision>, "reason": <reason>} so the caller can commit and the
-- platform.authorization_decisions row is kept (a raise would roll it back).

-- --- relation types (D5) -------------------------------------------------------------------------

INSERT INTO graph.relation_types VALUES
    ('in-domain',    '{*}', '{platform:domain}', 'entity -> life domain it belongs to'),
    ('serves-value', '{goals:goal,projects:project,habits:habit}', '{platform:value}', 'work -> value it serves'),
    ('measured-by',  '{goals:goal,habits:habit,projects:project}', '{platform:metric}', 'entity -> metric that shows progress'),
    ('defined-by',   '{operating:kpi}', '{platform:metric}', 'operating KPI -> catalog metric definition');
UPDATE graph.relation_types SET from_types = from_types || '{operating:kpi}'::text[]
WHERE relation = 'tracks-goal' AND NOT 'operating:kpi' = ANY (from_types);

CREATE FUNCTION graph.catalog_relations() RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['in-domain', 'serves-value', 'measured-by', 'defined-by']
$$;

-- --- tables (D2, D6, D7, D5 step 3) ---------------------------------------------------------------

CREATE TABLE graph.concepts (
    urn           text PRIMARY KEY,
    workspace_id  uuid NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('domain', 'value')),
    slug          text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    label         text NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
    description   text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    parent_urn    text REFERENCES graph.concepts (urn),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged', 'split', 'retired')),
    replaced_by   text[] NOT NULL DEFAULT '{}',
    introduced_in int NOT NULL,
    retired_in    int,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, kind, slug),
    CHECK (urn = 'empire:platform:' || kind || ':' || workspace_id::text || ':' || slug),
    CHECK (CASE status
        WHEN 'active' THEN cardinality(replaced_by) = 0 AND retired_in IS NULL
        WHEN 'merged' THEN cardinality(replaced_by) = 1 AND retired_in IS NOT NULL
        WHEN 'split' THEN cardinality(replaced_by) >= 2 AND retired_in IS NOT NULL
        ELSE cardinality(replaced_by) = 0 AND retired_in IS NOT NULL END)
);

CREATE TABLE graph.taxonomy_versions (
    workspace_id          uuid NOT NULL,
    version               int NOT NULL CHECK (version >= 1),
    note                  text NOT NULL CHECK (length(btrim(note)) BETWEEN 1 AND 2000),
    created_by            uuid,
    created_at            timestamptz NOT NULL DEFAULT now(),
    unclassified_snapshot jsonb NOT NULL,
    PRIMARY KEY (workspace_id, version)
);

-- Trusted register of readable sources: written by migrations only.
CREATE TABLE graph.metric_sources (
    cell            text NOT NULL,
    resource        text NOT NULL,
    classification  text NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    read_permission platform.permission NOT NULL,
    -- filters that more than one writer fills; a definition whose filter contains one needs source.dedupe
    multi_writer    jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(multi_writer) = 'array'),
    PRIMARY KEY (cell, resource)
);
INSERT INTO graph.metric_sources VALUES
    ('health', 'health-metrics', 'confidential', 'health.health-metric:read',
     '[{"metric_type": "sleep_hours"}, {"metric_type": "exercise_minutes"}, {"metric_type": "weight"}]'),
    ('diary', 'journal-entries', 'restricted', 'diary.journal-entry:read', '[]'),
    ('time', 'time-logs', 'internal', 'time.time-log:read', '[]');

CREATE TABLE graph.metric_definitions (
    urn             text PRIMARY KEY,
    workspace_id    uuid NOT NULL,
    key             text NOT NULL CHECK (key ~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*$' AND length(key) <= 80),
    label           text NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
    description     text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    domain_urn      text NOT NULL REFERENCES graph.concepts (urn),
    unit            text CHECK (length(unit) <= 20),
    scale           text NOT NULL CHECK (scale IN ('nominal', 'ordinal', 'interval', 'ratio')),
    value_range     jsonb,
    kind            text NOT NULL CHECK (kind IN ('leading', 'lagging', 'guardrail', 'descriptive')),
    objectivity     text NOT NULL CHECK (objectivity IN ('objective', 'subjective')),
    source          jsonb NOT NULL,
    aggregation     text NOT NULL,
    cadence         text NOT NULL CHECK (cadence IN ('daily', 'weekly', 'monthly', 'event')),
    criteria        jsonb NOT NULL DEFAULT '[]'::jsonb,
    target          jsonb,
    classification  text NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    read_permission platform.permission NOT NULL,
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    replaced_by     text,
    version         int NOT NULL CHECK (version >= 1),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, key),
    CHECK (urn = 'empire:platform:metric:' || workspace_id::text || ':' || key)
);
CREATE INDEX ON graph.metric_definitions (workspace_id, domain_urn);

CREATE TABLE graph.concept_revisions (
    id               uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id     uuid NOT NULL,
    urn              text NOT NULL,
    taxonomy_version int NOT NULL,
    row_data         jsonb NOT NULL,
    actor            uuid,
    execution_id     uuid,
    audit_event_id   uuid,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE graph.metric_revisions (LIKE graph.concept_revisions INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
ALTER TABLE graph.metric_revisions ADD PRIMARY KEY (id);
CREATE INDEX ON graph.concept_revisions (workspace_id, urn);
CREATE INDEX ON graph.metric_revisions (workspace_id, urn);

CREATE FUNCTION graph.reject_revision_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'catalog revisions are append-only' USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER concept_revisions_append_only BEFORE UPDATE OR DELETE ON graph.concept_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION graph.reject_revision_change();
CREATE TRIGGER metric_revisions_append_only BEFORE UPDATE OR DELETE ON graph.metric_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION graph.reject_revision_change();

-- Highest set_version applied per (subject, relation, asserting cell); .deleted writes its tombstone here.
CREATE TABLE graph.edge_versions (
    workspace_id uuid NOT NULL,
    from_urn     text NOT NULL,
    relation     text NOT NULL REFERENCES graph.relation_types (relation),
    asserted_by  text NOT NULL,
    version      bigint NOT NULL,
    PRIMARY KEY (workspace_id, from_urn, relation, asserted_by)
);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['concepts', 'taxonomy_versions', 'metric_definitions', 'concept_revisions',
                             'metric_revisions', 'edge_versions'] LOOP
        EXECUTE format('ALTER TABLE graph.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE graph.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY workspace_isolation ON graph.%I '
            'USING (workspace_id = platform.current_workspace()) '
            'WITH CHECK (workspace_id = platform.current_workspace())', t);
    END LOOP;
END
$$;

-- --- helpers ----------------------------------------------------------------------------------------

CREATE FUNCTION graph.catalog_fail(p_status int, p_code text, p_detail jsonb DEFAULT '{}'::jsonb) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = p_code, ERRCODE = 'CT' || p_status::text, DETAIL = p_detail::text;
END
$$;

CREATE FUNCTION graph.concept_urn(p_ws uuid, p_kind text, p_slug text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT 'empire:platform:' || p_kind || ':' || p_ws::text || ':' || p_slug
$$;

-- D1: the short form empire:platform:<kind>:<slug> becomes the canonical URN of the event's workspace.
CREATE FUNCTION graph.canonical_ref(p_ref text, p_ws uuid) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN m IS NULL THEN p_ref ELSE graph.concept_urn(p_ws, m[1], m[2]) END
    FROM (SELECT regexp_match(p_ref, '^empire:platform:(domain|value|metric):([a-z][a-z0-9._-]*)$') AS m) s
$$;

CREATE FUNCTION graph.taxonomy_lock_key(p_ws uuid) RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
    SELECT hashtextextended('graph-taxonomy:' || p_ws::text, 0)
$$;
CREATE FUNCTION graph.subject_lock_key(p_ws uuid, p_urn text) RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
    SELECT hashtextextended('graph-subject:' || p_ws::text || ':' || p_urn, 0)
$$;

CREATE FUNCTION graph.caller_scopes(p_caller jsonb) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(p_caller -> 'scopes') = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(p_caller -> 'scopes')) END
$$;

-- The actor's effective permission (delegation: scope and live grantor) and, for a scoped token, the scope.
CREATE FUNCTION graph.catalog_can(p_caller jsonb, p_permission text) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT p_caller IS NOT NULL AND p_caller ->> 'actor' IS NOT NULL AND p_permission IS NOT NULL
       AND platform.can((p_caller ->> 'actor')::uuid, p_permission, (p_caller ->> 'delegation')::uuid)
       AND (graph.caller_scopes(p_caller) IS NULL OR p_permission = ANY (graph.caller_scopes(p_caller)))
$$;

CREATE FUNCTION graph.catalog_workspace() RETURNS uuid LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF platform.current_workspace() IS NULL THEN
        PERFORM graph.catalog_fail(403, 'workspace_unbound');
    END IF;
    RETURN platform.current_workspace();
END
$$;

-- Writes go through the platform decision point with p_lock, so workspace policies (requires_approval) apply.
CREATE FUNCTION graph.catalog_authorize(p_caller jsonb, p_permission text, p_type text, p_id text) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_decision text;
    v_reason   text;
BEGIN
    IF graph.caller_scopes(p_caller) IS NOT NULL AND NOT p_permission = ANY (graph.caller_scopes(p_caller)) THEN
        PERFORM graph.catalog_fail(403, 'scope_insufficient', jsonb_build_object('permission', p_permission));
    END IF;
    SELECT a.decision, a.reason INTO v_decision, v_reason
    FROM platform.authorize((p_caller ->> 'actor')::uuid, platform.current_workspace(), p_permission, p_type, p_id,
                            platform.current_workspace(), (p_caller ->> 'delegation')::uuid,
                            (p_caller ->> 'on_behalf_of')::uuid, (p_caller ->> 'approval')::uuid,
                            coalesce(p_caller ->> 'channel', 'api'), true) a;
    IF v_decision IS DISTINCT FROM 'allow' THEN
        RETURN jsonb_build_object('status', 403, 'error', coalesce(v_decision, 'deny'), 'reason', v_reason);
    END IF;
    RETURN NULL;
END
$$;

-- One execution per catalog request (audit_events needs one); NULL when the actor is not an active member.
CREATE FUNCTION graph.catalog_execution(p_caller jsonb) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    v_actor uuid := (p_caller ->> 'actor')::uuid;
    v_id    uuid := (p_caller ->> 'execution')::uuid;
BEGIN
    IF v_id IS NOT NULL OR v_actor IS NULL THEN
        RETURN v_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM platform.memberships
                   WHERE workspace_id = platform.current_workspace() AND status = 'active'
                     AND identity_id = coalesce((p_caller ->> 'initiated_by')::uuid, v_actor)) THEN
        RETURN NULL;
    END IF;
    INSERT INTO platform.executions (workspace_id, initiated_by, executed_by, on_behalf_of, delegation_id,
                                     approval_id, channel, status, finished_at)
    VALUES (platform.current_workspace(), coalesce((p_caller ->> 'initiated_by')::uuid, v_actor), v_actor,
            coalesce((p_caller ->> 'on_behalf_of')::uuid, v_actor), (p_caller ->> 'delegation')::uuid,
            -- only an approval that exists here and was granted; a stray header value is not recorded
            (SELECT a.id FROM platform.approvals a
             WHERE a.id = (p_caller ->> 'approval')::uuid AND a.workspace_id = platform.current_workspace()
               AND a.decision = 'approved'),
            coalesce(p_caller ->> 'channel', 'api'), 'succeeded', now())
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;

CREATE FUNCTION graph.catalog_hash(p_row jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_row IS NULL THEN NULL ELSE encode(sha256(convert_to(p_row::text, 'UTF8')), 'hex') END
$$;

-- D2: hash audit (existing contract) plus a full revision row that stores the audit event id.
CREATE FUNCTION graph.catalog_revision(
    p_table text, p_caller jsonb, p_execution uuid, p_action text, p_type text, p_id text, p_urn text,
    p_before jsonb, p_after jsonb
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_audit   uuid;
    v_version int;
BEGIN
    IF p_execution IS NOT NULL THEN
        v_audit := platform.record_audit(p_execution, 'catalog', p_action, p_type, p_id,
                                         graph.catalog_hash(p_before), graph.catalog_hash(p_after));
    END IF;
    SELECT coalesce(max(version), 0) INTO v_version
    FROM graph.taxonomy_versions WHERE workspace_id = platform.current_workspace();
    IF p_table = 'concept' THEN
        INSERT INTO graph.concept_revisions (workspace_id, urn, taxonomy_version, row_data, actor, execution_id,
                                             audit_event_id)
        VALUES (platform.current_workspace(), p_urn, v_version, p_after, (p_caller ->> 'actor')::uuid,
                p_execution, v_audit);
    ELSE
        INSERT INTO graph.metric_revisions (workspace_id, urn, taxonomy_version, row_data, actor, execution_id,
                                            audit_event_id)
        VALUES (platform.current_workspace(), p_urn, v_version, p_after, (p_caller ->> 'actor')::uuid,
                p_execution, v_audit);
    END IF;
END
$$;

-- D4: counts of live in-domain edges to unclassified by cell/type/classification; restricted edges are
-- never counted (a consent may lapse later). No labels, no URNs.
CREATE FUNCTION graph.unclassified_snapshot(p_ws uuid) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('cell', cell, 'type', type, 'classification', classification,
                                                 'count', n) ORDER BY cell, type, classification), '[]'::jsonb)
    FROM (
        SELECT split_part(from_urn, ':', 2) AS cell, split_part(from_urn, ':', 3) AS type, classification,
               count(*) AS n
        FROM (
            SELECT DISTINCT ON (e.from_urn) e.from_urn, e.classification
            FROM graph.edges e
            WHERE e.workspace_id = p_ws AND e.relation = 'in-domain' AND e.retracted_at IS NULL
              AND graph.resolve(e.to_urn) = graph.concept_urn(p_ws, 'domain', 'unclassified')
            ORDER BY e.from_urn, graph.rank(e.classification) DESC
        ) entity
        WHERE classification <> 'restricted'
        GROUP BY 1, 2, 3
    ) s
$$;

CREATE FUNCTION graph.taxonomy_next(p_ws uuid) RETURNS int LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(version), 0) + 1 FROM graph.taxonomy_versions WHERE workspace_id = p_ws
$$;

CREATE FUNCTION graph.taxonomy_record(p_ws uuid, p_version int, p_note text, p_actor uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF p_note IS NULL OR length(btrim(p_note)) NOT BETWEEN 1 AND 2000 THEN
        PERFORM graph.catalog_fail(422, 'note_required');
    END IF;
    INSERT INTO graph.taxonomy_versions (workspace_id, version, note, created_by, unclassified_snapshot)
    VALUES (p_ws, p_version, p_note, p_actor, graph.unclassified_snapshot(p_ws));
END
$$;

CREATE FUNCTION graph.concept_json(p_c graph.concepts) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'urn', p_c.urn, 'kind', p_c.kind, 'slug', p_c.slug, 'label', p_c.label, 'description', p_c.description,
        'parent', (SELECT slug FROM graph.concepts WHERE urn = p_c.parent_urn),
        'status', p_c.status,
        'replaced_by', (SELECT coalesce(jsonb_agg(jsonb_build_object('kind', r.kind, 'slug', r.slug, 'urn', r.urn)
                                                  ORDER BY r.slug), '[]'::jsonb)
                        FROM graph.concepts r WHERE r.urn = ANY (p_c.replaced_by)),
        'introduced_in', p_c.introduced_in, 'retired_in', p_c.retired_in,
        'was', (SELECT coalesce(jsonb_agg(a.alias_urn ORDER BY a.alias_urn), '[]'::jsonb)
                FROM graph.aliases a WHERE a.urn = p_c.urn),
        'updated_at', p_c.updated_at)
$$;

CREATE FUNCTION graph.metric_json(p_m graph.metric_definitions) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'urn', p_m.urn, 'key', p_m.key, 'label', p_m.label, 'description', p_m.description,
        'domain', (SELECT slug FROM graph.concepts WHERE urn = p_m.domain_urn), 'domain_urn', p_m.domain_urn,
        'unit', p_m.unit, 'scale', p_m.scale, 'value_range', p_m.value_range, 'kind', p_m.kind,
        'objectivity', p_m.objectivity, 'source', p_m.source, 'aggregation', p_m.aggregation,
        'cadence', p_m.cadence, 'criteria', p_m.criteria, 'target', p_m.target,
        'classification', p_m.classification, 'read_permission', p_m.read_permission, 'status', p_m.status,
        'replaced_by', p_m.replaced_by, 'version', p_m.version, 'updated_at', p_m.updated_at)
$$;

-- D7b: the stored classification decides; protected definitions also need the source's read permission.
CREATE FUNCTION graph.metric_readable(p_caller jsonb, p_m graph.metric_definitions) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT graph.catalog_can(p_caller, 'catalog.metric-definition:read')
       AND (p_m.classification IN ('public', 'internal') OR graph.catalog_can(p_caller, p_m.read_permission))
$$;

CREATE FUNCTION graph.find_concept(p_ws uuid, p_kind text, p_slug text) RETURNS graph.concepts
LANGUAGE plpgsql AS $$
DECLARE
    v graph.concepts;
BEGIN
    SELECT * INTO v FROM graph.concepts WHERE workspace_id = p_ws AND kind = p_kind AND slug = p_slug FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM graph.catalog_fail(404, 'not_found', jsonb_build_object('kind', p_kind, 'slug', p_slug));
    END IF;
    RETURN v;
END
$$;

-- Parent: same kind (domains only), active, no cycle, depth of the whole chain at most 3.
CREATE FUNCTION graph.check_parent(p_ws uuid, p_kind text, p_self text, p_parent_slug text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_parent graph.concepts;
    v_depth  int;
    v_height int;
    v_cycle  boolean;
BEGIN
    IF p_parent_slug IS NULL THEN
        RETURN NULL;
    END IF;
    IF p_kind <> 'domain' OR p_self = graph.concept_urn(p_ws, 'domain', 'unclassified')
       OR p_parent_slug = 'unclassified' THEN
        PERFORM graph.catalog_fail(422, 'parent_not_allowed');
    END IF;
    SELECT * INTO v_parent FROM graph.concepts WHERE workspace_id = p_ws AND kind = p_kind AND slug = p_parent_slug;
    IF NOT FOUND OR v_parent.status <> 'active' THEN
        PERFORM graph.catalog_fail(422, 'parent_not_active', jsonb_build_object('parent', p_parent_slug));
    END IF;
    WITH RECURSIVE up AS (
        SELECT v_parent.urn AS urn, v_parent.parent_urn AS parent, 1 AS depth
        UNION ALL
        SELECT c.urn, c.parent_urn, up.depth + 1 FROM graph.concepts c JOIN up ON c.urn = up.parent
        WHERE up.depth < 10
    )
    SELECT max(depth), bool_or(urn = p_self) INTO v_depth, v_cycle FROM up;
    IF v_cycle THEN
        PERFORM graph.catalog_fail(422, 'parent_cycle');
    END IF;
    WITH RECURSIVE down AS (
        SELECT p_self AS urn, 1 AS height
        UNION ALL
        SELECT c.urn, down.height + 1 FROM graph.concepts c JOIN down ON c.parent_urn = down.urn
        WHERE down.height < 10
    )
    SELECT max(height) INTO v_height FROM down;
    IF v_depth + v_height > 3 THEN
        PERFORM graph.catalog_fail(422, 'parent_too_deep');
    END IF;
    RETURN v_parent.urn;
END
$$;

-- --- metric validation and storage (D6, D7) ---------------------------------------------------------

CREATE FUNCTION graph.aggregation_allowed(p_scale text, p_aggregation text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT p_aggregation = ANY (CASE p_scale
        WHEN 'nominal' THEN ARRAY['count', 'mode', 'last']
        WHEN 'ordinal' THEN ARRAY['count', 'mode', 'last', 'median', 'min', 'max']
        WHEN 'interval' THEN ARRAY['count', 'mode', 'last', 'median', 'min', 'max', 'mean']
        WHEN 'ratio' THEN ARRAY['count', 'mode', 'last', 'median', 'min', 'max', 'mean', 'sum']
        ELSE ARRAY[]::text[] END)
$$;

CREATE FUNCTION graph.json_number(p_value jsonb) RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(p_value) = 'number' THEN (p_value #>> '{}')::numeric END
$$;

-- One comparison ({window, op, value} plus level/action for criteria); range is the aggregate's range.
CREATE FUNCTION graph.check_threshold(p_item jsonb, p_is_criterion boolean, p_lo numeric, p_hi numeric) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v numeric := graph.json_number(p_item -> 'value');
BEGIN
    IF jsonb_typeof(p_item) <> 'object' OR coalesce(p_item ->> 'window', '') !~ '^[1-9][0-9]{0,2}[dwm]$'
       OR coalesce(p_item ->> 'op', '') NOT IN ('<', '<=', '>', '>=', '=') OR v IS NULL
       OR (p_is_criterion AND coalesce(p_item ->> 'level', '') NOT IN ('warn', 'alert'))
       OR (p_item ? 'action' AND jsonb_typeof(p_item -> 'action') <> 'string')
       OR length(coalesce(p_item ->> 'action', '')) > 500 THEN
        PERFORM graph.catalog_fail(422, 'invalid_threshold', jsonb_build_object('item', p_item));
    END IF;
    IF (p_lo IS NOT NULL AND v < p_lo) OR (p_hi IS NOT NULL AND v > p_hi) THEN
        PERFORM graph.catalog_fail(422, 'threshold_out_of_range',
                                   jsonb_build_object('value', v, 'min', p_lo, 'max', p_hi));
    END IF;
END
$$;

-- Validates one full definition and writes it (insert or update), its node, revision and audit.
-- p_existing is the locked current row (NULL for a create). Callers hold the taxonomy lock and have
-- already checked permissions; the seed calls it directly so seeded rows pass the same rules as PUT.
CREATE FUNCTION graph.store_metric(
    p_caller jsonb, p_execution uuid, p_key text, p_body jsonb, p_existing graph.metric_definitions
) RETURNS graph.metric_definitions LANGUAGE plpgsql AS $$
DECLARE
    v_ws       uuid := platform.current_workspace();
    v_source   jsonb := p_body -> 'source';
    v_dedupe   jsonb := p_body -> 'source' -> 'dedupe';
    v_filter   jsonb := p_body -> 'source' -> 'filter';
    v_reg      graph.metric_sources;
    v_domain   graph.concepts;
    v          graph.metric_definitions;
    v_lo       numeric;
    v_hi       numeric;
    v_plo      numeric;
    v_phi      numeric;
    v_item     jsonb;
    v_before   jsonb;
BEGIN
    IF jsonb_typeof(p_body) IS DISTINCT FROM 'object' OR jsonb_typeof(v_source) IS DISTINCT FROM 'object' THEN
        PERFORM graph.catalog_fail(422, 'invalid_body');
    END IF;
    SELECT * INTO v_reg FROM graph.metric_sources WHERE cell = v_source ->> 'cell' AND resource = v_source ->> 'resource';
    IF NOT FOUND THEN
        PERFORM graph.catalog_fail(422, 'unknown_source');
    END IF;
    IF split_part(p_key, '.', 1) <> v_reg.cell THEN
        PERFORM graph.catalog_fail(422, 'key_cell_mismatch', jsonb_build_object('cell', v_reg.cell));
    END IF;
    SELECT * INTO v_domain FROM graph.concepts WHERE workspace_id = v_ws AND kind = 'domain' AND slug = p_body ->> 'domain';
    IF NOT FOUND OR v_domain.status <> 'active' THEN
        PERFORM graph.catalog_fail(422, 'domain_not_active', jsonb_build_object('domain', p_body ->> 'domain'));
    END IF;

    v.urn := graph.concept_urn(v_ws, 'metric', p_key);
    v.workspace_id := v_ws;
    v.key := p_key;
    v.label := p_body ->> 'label';
    v.description := coalesce(p_body ->> 'description', '');
    v.domain_urn := v_domain.urn;
    v.unit := p_body ->> 'unit';
    v.scale := p_body ->> 'scale';
    v.value_range := nullif(p_body -> 'value_range', 'null'::jsonb);
    v.kind := p_body ->> 'kind';
    v.objectivity := p_body ->> 'objectivity';
    v.source := v_source - 'read_permission';
    v.aggregation := p_body ->> 'aggregation';
    v.cadence := p_body ->> 'cadence';
    v.criteria := coalesce(nullif(p_body -> 'criteria', 'null'::jsonb), '[]'::jsonb);
    v.target := nullif(p_body -> 'target', 'null'::jsonb);
    v.classification := coalesce(p_body ->> 'classification', p_existing.classification, v_reg.classification);
    v.read_permission := v_reg.read_permission;  -- copied from the register, never from the writer
    v.status := coalesce(p_body ->> 'status', 'active');
    v.replaced_by := p_body ->> 'replaced_by';

    IF v.label IS NULL OR length(v.label) NOT BETWEEN 1 AND 200 OR length(v.description) > 4000 THEN
        PERFORM graph.catalog_fail(422, 'invalid_label');
    END IF;
    IF v.scale IS NULL OR v.scale NOT IN ('nominal', 'ordinal', 'interval', 'ratio')
       OR v.kind IS NULL OR v.kind NOT IN ('leading', 'lagging', 'guardrail', 'descriptive')
       OR v.objectivity IS NULL OR v.objectivity NOT IN ('objective', 'subjective')
       OR v.cadence IS NULL OR v.cadence NOT IN ('daily', 'weekly', 'monthly', 'event')
       OR v.status NOT IN ('active', 'retired') OR length(v.unit) > 20
       OR v.classification NOT IN ('public', 'internal', 'confidential', 'restricted') THEN
        PERFORM graph.catalog_fail(422, 'invalid_field');
    END IF;
    IF graph.rank(v.classification) < graph.rank(v_reg.classification) THEN
        PERFORM graph.catalog_fail(422, 'classification_below_source',
                                   jsonb_build_object('minimum', v_reg.classification));
    END IF;
    IF v.aggregation IS NULL OR NOT graph.aggregation_allowed(v.scale, v.aggregation) THEN
        PERFORM graph.catalog_fail(422, 'aggregation_not_allowed',
                                   jsonb_build_object('scale', v.scale, 'aggregation', v.aggregation));
    END IF;
    -- source pointer
    IF jsonb_typeof(v_source -> 'field') IS DISTINCT FROM 'string' OR v_source ->> 'field' !~ '\S'
       OR (v_filter IS NOT NULL AND jsonb_typeof(v_filter) <> 'object')
       OR (v_source ? 'time_field' AND (jsonb_typeof(v_source -> 'time_field') <> 'string'
                                        OR v_source ->> 'time_field' !~ '\S')) THEN
        PERFORM graph.catalog_fail(422, 'invalid_source');
    END IF;
    IF v.aggregation = 'last' AND v_source ->> 'time_field' IS NULL THEN
        PERFORM graph.catalog_fail(422, 'time_field_required');
    END IF;
    IF v_dedupe IS NOT NULL THEN
        IF jsonb_typeof(v_dedupe) <> 'object' OR v_dedupe ->> 'by' IS DISTINCT FROM 'day'
           OR jsonb_typeof(v_dedupe -> 'field') IS DISTINCT FROM 'string' OR v_dedupe ->> 'field' !~ '\S'
           OR jsonb_typeof(v_dedupe -> 'prefer') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_dedupe -> 'prefer') = 0
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(v_dedupe -> 'prefer') e
                      WHERE jsonb_typeof(e.value) <> 'string' OR e.value #>> '{}' !~ '\S')
           OR coalesce(v_dedupe ->> 'combine', '') NOT IN ('sum', 'last', 'max') THEN
            PERFORM graph.catalog_fail(422, 'invalid_dedupe');
        END IF;
        IF v_dedupe ->> 'tz' IS NULL OR NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = v_dedupe ->> 'tz') THEN
            PERFORM graph.catalog_fail(422, 'dedupe_tz_required');
        END IF;
        IF v_source ->> 'time_field' IS NULL THEN
            PERFORM graph.catalog_fail(422, 'time_field_required');
        END IF;
    ELSIF jsonb_array_length(v_reg.multi_writer) > 0 AND NOT EXISTS (
        -- the filter rules out every multi-writer series only when it pins one of their keys to another value
        SELECT 1 FROM (SELECT DISTINCT jsonb_object_keys(w.value) AS k
                       FROM jsonb_array_elements(v_reg.multi_writer) w) keys
        WHERE coalesce(v_filter, '{}'::jsonb) ? keys.k
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_reg.multi_writer) w
                          WHERE w.value -> keys.k = v_filter -> keys.k)) THEN
        PERFORM graph.catalog_fail(422, 'dedupe_required');
    END IF;
    -- value_range: bounds of one raw observation
    IF v.value_range IS NOT NULL AND (
        jsonb_typeof(v.value_range) <> 'object'
        OR (v.value_range ? 'min' AND graph.json_number(v.value_range -> 'min') IS NULL)
        OR (v.value_range ? 'max' AND graph.json_number(v.value_range -> 'max') IS NULL)
        OR graph.json_number(v.value_range -> 'min') > graph.json_number(v.value_range -> 'max')) THEN
        PERFORM graph.catalog_fail(422, 'invalid_value_range');
    END IF;
    -- D7 ranges: per period after dedupe.combine, then the aggregate
    v_plo := graph.json_number(v.value_range -> 'min');
    v_phi := graph.json_number(v.value_range -> 'max');
    IF v_dedupe ->> 'combine' = 'sum' THEN
        v_plo := CASE WHEN v_plo >= 0 THEN 0 END;
        v_phi := NULL;
    END IF;
    IF v.aggregation = 'sum' THEN
        v_lo := CASE WHEN graph.json_number(v.value_range -> 'min') >= 0 THEN 0 END;
        v_hi := NULL;
    ELSIF v.aggregation = 'count' THEN
        v_lo := 0;
        v_hi := NULL;
    ELSE
        v_lo := v_plo;
        v_hi := v_phi;
    END IF;
    IF jsonb_typeof(v.criteria) <> 'array' OR (v.target IS NOT NULL AND jsonb_typeof(v.target) <> 'object') THEN
        PERFORM graph.catalog_fail(422, 'invalid_threshold');
    END IF;
    IF v.scale = 'nominal' AND (jsonb_array_length(v.criteria) > 0 OR v.target IS NOT NULL) THEN
        PERFORM graph.catalog_fail(422, 'comparison_on_nominal');
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(v.criteria) LOOP
        PERFORM graph.check_threshold(v_item, true, v_lo, v_hi);
    END LOOP;
    IF v.target IS NOT NULL THEN
        PERFORM graph.check_threshold(v.target, false, v_lo, v_hi);
    END IF;

    -- version: any change other than label/description bumps it
    IF p_existing.urn IS NULL THEN
        v.version := 1;
    ELSE
        v_before := to_jsonb(p_existing);
        v.version := p_existing.version + CASE
            WHEN (to_jsonb(v) - '{label,description,version,updated_at}'::text[])
                 = (v_before - '{label,description,version,updated_at}'::text[]) THEN 0 ELSE 1 END;
    END IF;
    v.updated_at := now();

    INSERT INTO graph.metric_definitions VALUES (v.*)
    ON CONFLICT (urn) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description,
        domain_urn = EXCLUDED.domain_urn, unit = EXCLUDED.unit, scale = EXCLUDED.scale,
        value_range = EXCLUDED.value_range, kind = EXCLUDED.kind, objectivity = EXCLUDED.objectivity,
        source = EXCLUDED.source, aggregation = EXCLUDED.aggregation, cadence = EXCLUDED.cadence,
        criteria = EXCLUDED.criteria, target = EXCLUDED.target, classification = EXCLUDED.classification,
        read_permission = EXCLUDED.read_permission, status = EXCLUDED.status, replaced_by = EXCLUDED.replaced_by,
        version = EXCLUDED.version, updated_at = EXCLUDED.updated_at;
    INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version)
    VALUES (v.urn, v_ws, 'platform', 'metric', CASE WHEN v.classification = 'restricted' THEN NULL ELSE v.label END,
            v.classification, CASE WHEN v.status = 'active' THEN 'active' ELSE 'archived' END, v.version)
    ON CONFLICT (urn) DO UPDATE SET label = EXCLUDED.label, classification = EXCLUDED.classification,
        lifecycle = EXCLUDED.lifecycle, version = EXCLUDED.version, updated_at = now();
    PERFORM graph.catalog_revision('metric', p_caller, p_execution, 'catalog.metric-definition:write',
                                   'catalog.metric-definition', p_key, v.urn, v_before, to_jsonb(v));
    RETURN v;
END
$$;

-- Moves one metric to another domain (merge, split, retire): version + 1, revision, audit.
CREATE FUNCTION graph.move_metric(p_caller jsonb, p_execution uuid, p_m graph.metric_definitions, p_domain text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v graph.metric_definitions;
BEGIN
    UPDATE graph.metric_definitions SET domain_urn = p_domain, version = version + 1, updated_at = now()
    WHERE urn = p_m.urn RETURNING * INTO v;
    UPDATE graph.nodes SET version = v.version, updated_at = now() WHERE urn = v.urn;
    PERFORM graph.catalog_revision('metric', p_caller, p_execution, 'catalog.metric-definition:write',
                                   'catalog.metric-definition', v.key, v.urn, to_jsonb(p_m), to_jsonb(v));
END
$$;

-- D3: split and retire need metric_moves covering every metric on the domain. Keys the caller cannot read
-- are refused like unknown keys and counted, unnamed, in hidden_dependencies.
CREATE FUNCTION graph.apply_metric_moves(
    p_caller jsonb, p_execution uuid, p_source text, p_moves jsonb
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    v_ws      uuid := platform.current_workspace();
    v_key     text;
    v_slug    text;
    v_target  graph.concepts;
    v_m       graph.metric_definitions;
    v_missing text[];
    v_hidden  int;
BEGIN
    IF p_moves IS NOT NULL AND jsonb_typeof(p_moves) <> 'object' THEN
        PERFORM graph.catalog_fail(422, 'invalid_metric_moves');
    END IF;
    FOR v_key, v_slug IN SELECT key, value FROM jsonb_each_text(coalesce(p_moves, '{}'::jsonb)) LOOP
        SELECT * INTO v_m FROM graph.metric_definitions WHERE workspace_id = v_ws AND key = v_key FOR UPDATE;
        IF NOT FOUND OR v_m.domain_urn <> p_source OR NOT graph.metric_readable(p_caller, v_m) THEN
            PERFORM graph.catalog_fail(422, 'unknown_metric', jsonb_build_object('key', v_key));
        END IF;
        SELECT * INTO v_target FROM graph.concepts WHERE workspace_id = v_ws AND kind = 'domain' AND slug = v_slug;
        IF NOT FOUND OR v_target.status <> 'active' OR v_target.urn = p_source THEN
            PERFORM graph.catalog_fail(422, 'invalid_move_target', jsonb_build_object('key', v_key, 'domain', v_slug));
        END IF;
    END LOOP;
    SELECT coalesce(array_agg(m.key ORDER BY m.key) FILTER (WHERE graph.metric_readable(p_caller, m)), '{}'),
           count(*) FILTER (WHERE NOT graph.metric_readable(p_caller, m))
    INTO v_missing, v_hidden
    FROM graph.metric_definitions m
    WHERE m.workspace_id = v_ws AND m.domain_urn = p_source AND NOT coalesce(p_moves, '{}'::jsonb) ? m.key;
    IF cardinality(v_missing) > 0 OR v_hidden > 0 THEN
        PERFORM graph.catalog_fail(409, 'metrics_depend',
                                   jsonb_build_object('metrics', to_jsonb(v_missing), 'hidden_dependencies', v_hidden));
    END IF;
    FOR v_key, v_slug IN SELECT key, value FROM jsonb_each_text(coalesce(p_moves, '{}'::jsonb)) LOOP
        SELECT * INTO v_m FROM graph.metric_definitions WHERE workspace_id = v_ws AND key = v_key;
        PERFORM graph.move_metric(p_caller, p_execution, v_m, graph.concept_urn(v_ws, 'domain', v_slug));
    END LOOP;
    RETURN to_jsonb(ARRAY(SELECT jsonb_object_keys(coalesce(p_moves, '{}'::jsonb)) ORDER BY 1));
END
$$;

-- Shared checks for merge, split and retire of p_c (already locked).
CREATE FUNCTION graph.check_retirable(p_c graph.concepts) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF p_c.kind = 'domain' AND p_c.slug = 'unclassified' THEN
        PERFORM graph.catalog_fail(409, 'protected_concept');
    END IF;
    IF p_c.status <> 'active' THEN
        PERFORM graph.catalog_fail(409, 'not_active', jsonb_build_object('status', p_c.status));
    END IF;
    IF EXISTS (SELECT 1 FROM graph.concepts WHERE parent_urn = p_c.urn AND status = 'active') THEN
        PERFORM graph.catalog_fail(409, 'has_children');
    END IF;
END
$$;

-- --- entry points (D8) ------------------------------------------------------------------------------
-- p_caller: {"actor", "delegation", "scopes", "on_behalf_of", "approval", "execution", "channel"} from the
-- verified token; "scopes" is null for an unscoped token.

CREATE FUNCTION graph.catalog_list_concepts(p_caller jsonb, p_kind text DEFAULT NULL, p_status text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws uuid := graph.catalog_workspace();
BEGIN
    IF NOT graph.catalog_can(p_caller, 'catalog.concept:read') THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    RETURN (SELECT coalesce(jsonb_agg(graph.concept_json(c) ORDER BY c.kind, c.slug), '[]'::jsonb)
            FROM graph.concepts c
            WHERE c.workspace_id = v_ws AND (p_kind IS NULL OR c.kind = p_kind)
              AND (p_status IS NULL OR c.status = p_status));
END
$$;

CREATE FUNCTION graph.catalog_create_concept(
    p_caller jsonb, p_kind text, p_slug text, p_label text, p_description text, p_parent text, p_note text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws      uuid := graph.catalog_workspace();
    v_exec    uuid;
    v_version int;
    v         graph.concepts;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('domain', 'value') OR p_slug IS NULL OR p_slug !~ '^[a-z][a-z0-9-]{1,40}$'
       OR p_label IS NULL OR length(p_label) NOT BETWEEN 1 AND 200 OR length(p_description) > 4000 THEN
        PERFORM graph.catalog_fail(422, 'invalid_concept');
    END IF;
    v_denied := graph.catalog_authorize(p_caller, 'catalog.concept:write', 'catalog.concept', p_kind || '/' || p_slug);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    IF EXISTS (SELECT 1 FROM graph.concepts WHERE workspace_id = v_ws AND kind = p_kind AND slug = p_slug) THEN
        PERFORM graph.catalog_fail(409, 'exists');
    END IF;
    v_exec := graph.catalog_execution(p_caller);
    v_version := graph.taxonomy_next(v_ws);
    INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version)
    VALUES (graph.concept_urn(v_ws, p_kind, p_slug), v_ws, 'platform', p_kind, p_label, 'internal', 'active', 1)
    ON CONFLICT (urn) DO UPDATE SET label = EXCLUDED.label, lifecycle = 'active', updated_at = now();
    INSERT INTO graph.concepts (urn, workspace_id, kind, slug, label, description, parent_urn, introduced_in)
    VALUES (graph.concept_urn(v_ws, p_kind, p_slug), v_ws, p_kind, p_slug, p_label, coalesce(p_description, ''),
            graph.check_parent(v_ws, p_kind, graph.concept_urn(v_ws, p_kind, p_slug), p_parent), v_version)
    RETURNING * INTO v;
    PERFORM graph.taxonomy_record(v_ws, v_version, p_note, (p_caller ->> 'actor')::uuid);
    PERFORM graph.catalog_revision('concept', p_caller, v_exec, 'catalog.concept:write', 'catalog.concept',
                                   p_kind || '/' || p_slug, v.urn, NULL, to_jsonb(v));
    RETURN graph.concept_json(v);
END
$$;

-- p_patch: {"label"?, "description"?, "parent"? (slug or null), "note"? (required when parent changes)}
CREATE FUNCTION graph.catalog_update_concept(p_caller jsonb, p_kind text, p_slug text, p_patch jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws      uuid := graph.catalog_workspace();
    v_exec    uuid;
    v_before  graph.concepts;
    v         graph.concepts;
    v_version int;
BEGIN
    IF jsonb_typeof(p_patch) IS DISTINCT FROM 'object' THEN
        PERFORM graph.catalog_fail(422, 'invalid_patch');
    END IF;
    v_denied := graph.catalog_authorize(p_caller, 'catalog.concept:write', 'catalog.concept', p_kind || '/' || p_slug);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    v_before := graph.find_concept(v_ws, p_kind, p_slug);
    v := v_before;
    v.label := coalesce(p_patch ->> 'label', v.label);
    v.description := coalesce(p_patch ->> 'description', v.description);
    IF length(v.label) NOT BETWEEN 1 AND 200 OR length(v.description) > 4000 THEN
        PERFORM graph.catalog_fail(422, 'invalid_concept');
    END IF;
    v_exec := graph.catalog_execution(p_caller);
    IF p_patch ? 'parent' THEN
        IF v.status <> 'active' THEN
            PERFORM graph.catalog_fail(409, 'not_active');
        END IF;
        v.parent_urn := graph.check_parent(v_ws, p_kind, v.urn, p_patch ->> 'parent');
        IF v.parent_urn IS DISTINCT FROM v_before.parent_urn THEN
            v_version := graph.taxonomy_next(v_ws);
            PERFORM graph.taxonomy_record(v_ws, v_version, p_patch ->> 'note', (p_caller ->> 'actor')::uuid);
        END IF;
    END IF;
    UPDATE graph.concepts SET label = v.label, description = v.description, parent_urn = v.parent_urn,
        updated_at = now()
    WHERE urn = v.urn RETURNING * INTO v;
    UPDATE graph.nodes SET label = v.label, updated_at = now() WHERE urn = v.urn;
    PERFORM graph.catalog_revision('concept', p_caller, v_exec, 'catalog.concept:write', 'catalog.concept',
                                   p_kind || '/' || p_slug, v.urn, to_jsonb(v_before), to_jsonb(v));
    RETURN graph.concept_json(v);
END
$$;

CREATE FUNCTION graph.catalog_merge(p_caller jsonb, p_kind text, p_slug text, p_into text, p_note text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws      uuid := graph.catalog_workspace();
    v_exec    uuid;
    v_version int;
    v_source  graph.concepts;
    v_target  graph.concepts;
    v         graph.concepts;
    v_m       graph.metric_definitions;
    v_moved   text[] := '{}';
BEGIN
    v_denied := graph.catalog_authorize(p_caller, 'catalog.concept:admin', 'catalog.concept', p_kind || '/' || p_slug);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    v_source := graph.find_concept(v_ws, p_kind, p_slug);
    IF p_into IS NOT DISTINCT FROM p_slug THEN
        PERFORM graph.catalog_fail(422, 'same_target');
    END IF;
    v_target := graph.find_concept(v_ws, p_kind, p_into);
    PERFORM graph.check_retirable(v_source);
    IF v_target.status <> 'active' THEN
        PERFORM graph.catalog_fail(409, 'target_not_active', jsonb_build_object('status', v_target.status));
    END IF;
    v_exec := graph.catalog_execution(p_caller);
    v_version := graph.taxonomy_next(v_ws);
    -- aliases stay one hop: everything that pointed at the source now points at the target
    UPDATE graph.aliases SET urn = v_target.urn WHERE workspace_id = v_ws AND urn = v_source.urn;
    INSERT INTO graph.aliases (alias_urn, workspace_id, urn) VALUES (v_source.urn, v_ws, v_target.urn);
    UPDATE graph.concepts SET status = 'merged', replaced_by = ARRAY[v_target.urn], retired_in = v_version,
        updated_at = now()
    WHERE urn = v_source.urn RETURNING * INTO v;
    UPDATE graph.nodes SET lifecycle = 'archived', updated_at = now() WHERE urn = v_source.urn;
    PERFORM graph.taxonomy_record(v_ws, v_version, p_note, (p_caller ->> 'actor')::uuid);
    FOR v_m IN SELECT * FROM graph.metric_definitions WHERE workspace_id = v_ws AND domain_urn = v_source.urn
               ORDER BY key FOR UPDATE LOOP
        PERFORM graph.move_metric(p_caller, v_exec, v_m, v_target.urn);
        IF graph.metric_readable(p_caller, v_m) THEN
            v_moved := v_moved || v_m.key;
        END IF;
    END LOOP;
    PERFORM graph.catalog_revision('concept', p_caller, v_exec, 'catalog.concept:admin', 'catalog.concept',
                                   p_kind || '/' || p_slug, v.urn, to_jsonb(v_source), to_jsonb(v));
    RETURN jsonb_build_object('concept', graph.concept_json(v_target), 'merged', graph.concept_json(v),
                              'moved_metrics', to_jsonb(v_moved), 'version', v_version);
END
$$;

CREATE FUNCTION graph.catalog_split(
    p_caller jsonb, p_kind text, p_slug text, p_into text[], p_note text, p_metric_moves jsonb DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws      uuid := graph.catalog_workspace();
    v_exec    uuid;
    v_version int;
    v_source  graph.concepts;
    v_target  graph.concepts;
    v_targets text[] := '{}';
    v_slug    text;
    v         graph.concepts;
    v_moved   jsonb;
BEGIN
    v_denied := graph.catalog_authorize(p_caller, 'catalog.concept:admin', 'catalog.concept', p_kind || '/' || p_slug);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    v_source := graph.find_concept(v_ws, p_kind, p_slug);
    IF p_slug = ANY (coalesce(p_into, '{}')) THEN
        PERFORM graph.catalog_fail(422, 'same_target');
    END IF;
    IF (SELECT count(DISTINCT s) FROM unnest(coalesce(p_into, '{}')) s) < 2 THEN
        PERFORM graph.catalog_fail(422, 'split_needs_two_targets');
    END IF;
    PERFORM graph.check_retirable(v_source);
    FOR v_slug IN SELECT DISTINCT s FROM unnest(p_into) s ORDER BY 1 LOOP
        v_target := graph.find_concept(v_ws, p_kind, v_slug);
        IF v_target.status <> 'active' THEN
            PERFORM graph.catalog_fail(409, 'target_not_active', jsonb_build_object('slug', v_slug));
        END IF;
        v_targets := v_targets || v_target.urn;
    END LOOP;
    v_exec := graph.catalog_execution(p_caller);
    v_version := graph.taxonomy_next(v_ws);
    PERFORM graph.taxonomy_record(v_ws, v_version, p_note, (p_caller ->> 'actor')::uuid);
    v_moved := graph.apply_metric_moves(p_caller, v_exec, v_source.urn, p_metric_moves);
    UPDATE graph.concepts SET status = 'split', replaced_by = v_targets, retired_in = v_version, updated_at = now()
    WHERE urn = v_source.urn RETURNING * INTO v;
    UPDATE graph.nodes SET lifecycle = 'archived', updated_at = now() WHERE urn = v_source.urn;
    PERFORM graph.catalog_revision('concept', p_caller, v_exec, 'catalog.concept:admin', 'catalog.concept',
                                   p_kind || '/' || p_slug, v.urn, to_jsonb(v_source), to_jsonb(v));
    RETURN jsonb_build_object('concept', graph.concept_json(v), 'moved_metrics', v_moved, 'version', v_version);
END
$$;

CREATE FUNCTION graph.catalog_retire(
    p_caller jsonb, p_kind text, p_slug text, p_note text, p_metric_moves jsonb DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws      uuid := graph.catalog_workspace();
    v_exec    uuid;
    v_version int;
    v_source  graph.concepts;
    v         graph.concepts;
    v_moved   jsonb;
BEGIN
    v_denied := graph.catalog_authorize(p_caller, 'catalog.concept:admin', 'catalog.concept', p_kind || '/' || p_slug);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    v_source := graph.find_concept(v_ws, p_kind, p_slug);
    PERFORM graph.check_retirable(v_source);
    v_exec := graph.catalog_execution(p_caller);
    v_version := graph.taxonomy_next(v_ws);
    PERFORM graph.taxonomy_record(v_ws, v_version, p_note, (p_caller ->> 'actor')::uuid);
    v_moved := graph.apply_metric_moves(p_caller, v_exec, v_source.urn, p_metric_moves);
    UPDATE graph.concepts SET status = 'retired', retired_in = v_version, updated_at = now()
    WHERE urn = v_source.urn RETURNING * INTO v;
    UPDATE graph.nodes SET lifecycle = 'archived', updated_at = now() WHERE urn = v_source.urn;
    PERFORM graph.catalog_revision('concept', p_caller, v_exec, 'catalog.concept:admin', 'catalog.concept',
                                   p_kind || '/' || p_slug, v.urn, to_jsonb(v_source), to_jsonb(v));
    RETURN jsonb_build_object('concept', graph.concept_json(v), 'moved_metrics', v_moved, 'version', v_version);
END
$$;

CREATE FUNCTION graph.catalog_list_metrics(p_caller jsonb, p_domain text DEFAULT NULL, p_kind text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws uuid := graph.catalog_workspace();
BEGIN
    IF NOT graph.catalog_can(p_caller, 'catalog.metric-definition:read') THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    RETURN (SELECT coalesce(jsonb_agg(graph.metric_json(m) ORDER BY m.key), '[]'::jsonb)
            FROM graph.metric_definitions m
            WHERE m.workspace_id = v_ws AND graph.metric_readable(p_caller, m)
              AND (p_domain IS NULL OR m.domain_urn = graph.concept_urn(v_ws, 'domain', p_domain))
              AND (p_kind IS NULL OR m.kind = p_kind));
END
$$;

CREATE FUNCTION graph.catalog_get_metric(p_caller jsonb, p_key text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws uuid := graph.catalog_workspace();
    v    graph.metric_definitions;
BEGIN
    SELECT * INTO v FROM graph.metric_definitions WHERE workspace_id = v_ws AND key = p_key;
    IF NOT FOUND OR NOT graph.metric_readable(p_caller, v) THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    RETURN graph.metric_json(v);
END
$$;

-- Full-row PUT. Order matters for non-disclosure: write permission, then the source's read permission
-- (404 whether or not the key exists), then the lock, the stored row and if_version.
CREATE FUNCTION graph.catalog_put_metric(p_caller jsonb, p_key text, p_body jsonb, p_if_version int DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_denied  jsonb;
    v_ws       uuid := graph.catalog_workspace();
    v_exec     uuid;
    v_reg      graph.metric_sources;
    v_existing graph.metric_definitions;
    v          graph.metric_definitions;
BEGIN
    IF p_key IS NULL OR p_key !~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*$' OR length(p_key) > 80 THEN
        PERFORM graph.catalog_fail(422, 'invalid_key');
    END IF;
    v_denied := graph.catalog_authorize(p_caller, 'catalog.metric-definition:write', 'catalog.metric-definition', p_key);
    IF v_denied IS NOT NULL THEN
        RETURN v_denied;
    END IF;
    SELECT * INTO v_reg FROM graph.metric_sources
    WHERE cell = p_body -> 'source' ->> 'cell' AND resource = p_body -> 'source' ->> 'resource';
    IF NOT FOUND THEN
        PERFORM graph.catalog_fail(422, 'unknown_source');
    END IF;
    IF NOT graph.catalog_can(p_caller, v_reg.read_permission) THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    IF split_part(p_key, '.', 1) <> v_reg.cell THEN  -- before the lookup: same answer whether the key exists
        PERFORM graph.catalog_fail(422, 'key_cell_mismatch', jsonb_build_object('cell', v_reg.cell));
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    SELECT * INTO v_existing FROM graph.metric_definitions WHERE workspace_id = v_ws AND key = p_key FOR UPDATE;
    IF FOUND THEN
        IF NOT graph.catalog_can(p_caller, v_existing.read_permission) THEN
            PERFORM graph.catalog_fail(404, 'not_found');
        END IF;
        IF p_if_version IS DISTINCT FROM v_existing.version THEN
            PERFORM graph.catalog_fail(409, 'version_conflict', jsonb_build_object('version', v_existing.version));
        END IF;
    END IF;
    v_exec := graph.catalog_execution(p_caller);
    v := graph.store_metric(p_caller, v_exec, p_key, p_body, v_existing);
    RETURN graph.metric_json(v);
END
$$;

-- D4: current state, filtered before counting with the graph read rules and the token's scopes.
CREATE FUNCTION graph.catalog_review(p_caller jsonb)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws           uuid := graph.catalog_workspace();
    v_unclassified text := graph.concept_urn(v_ws, 'domain', 'unclassified');
BEGIN
    IF NOT graph.catalog_can(p_caller, 'catalog.concept:read') THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    RETURN (
        WITH live AS (
            SELECT e.*, graph.resolve(e.to_urn) AS target
            FROM graph.edges e
            WHERE e.workspace_id = v_ws AND e.relation IN ('in-domain', 'serves-value') AND e.retracted_at IS NULL
        ),
        visible AS (
            SELECT l.*, n.cell, n.type, n.label
            FROM live l JOIN graph.nodes n ON n.urn = l.from_urn
            WHERE ((l.relation = 'in-domain' AND l.target = v_unclassified)
                   OR EXISTS (SELECT 1 FROM graph.concepts c WHERE c.urn = l.target AND c.status = 'split'))
              AND graph.node_visible(l.from_urn, (p_caller ->> 'actor')::uuid, (p_caller ->> 'delegation')::uuid)
              AND (graph.caller_scopes(p_caller) IS NULL
                   OR n.cell || '.' || n.type || ':read' = ANY (graph.caller_scopes(p_caller)))
              AND (l.classification <> 'restricted' OR EXISTS (
                   SELECT 1 FROM graph.consents c
                   WHERE c.relation = l.relation AND c.subject_urn IN (l.from_urn, l.to_urn)
                     AND c.revoked_at IS NULL AND c.expires_at > now()))
        ),
        placed AS (  -- fallback edges are listed under the concept the cell asked for
            SELECT DISTINCT ON (p.concept_urn, v.from_urn) v.*, p.concept_urn
            FROM visible v
            CROSS JOIN LATERAL (
                SELECT graph.resolve(r.value) AS concept_urn
                FROM jsonb_array_elements_text(
                    CASE WHEN v.target = v_unclassified AND jsonb_typeof(v.provenance -> 'requested') = 'array'
                         THEN v.provenance -> 'requested' ELSE '[]'::jsonb END) r
                UNION
                SELECT v.target
                WHERE v.target <> v_unclassified OR jsonb_typeof(v.provenance -> 'requested') IS DISTINCT FROM 'array'
                   OR coalesce((v.provenance ->> 'direct')::boolean, false)
            ) p
            ORDER BY p.concept_urn, v.from_urn, v.valid_from DESC
        ),
        grouped AS (
            SELECT concept_urn, cell, type, count(*) AS n,
                   (array_agg(label ORDER BY valid_from DESC) FILTER (WHERE label IS NOT NULL))[1:5] AS labels
            FROM placed GROUP BY concept_urn, cell, type
        )
        SELECT jsonb_build_object('items', coalesce(jsonb_agg(jsonb_build_object(
                   'concept', jsonb_build_object('urn', c.urn, 'kind', c.kind, 'slug', c.slug, 'label', c.label,
                                                 'status', c.status),
                   'cell', g.cell, 'type', g.type, 'count', g.n, 'recent_labels', to_jsonb(coalesce(g.labels, '{}')))
                   ORDER BY c.slug, g.cell, g.type), '[]'::jsonb))
        FROM grouped g JOIN graph.concepts c ON c.urn = g.concept_urn
    );
END
$$;

CREATE FUNCTION graph.catalog_versions(p_caller jsonb)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws uuid := graph.catalog_workspace();
BEGIN
    IF NOT graph.catalog_can(p_caller, 'catalog.concept:read') THEN
        PERFORM graph.catalog_fail(404, 'not_found');
    END IF;
    RETURN (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'version', t.version, 'note', t.note, 'created_by', t.created_by, 'created_at', t.created_at,
                'restricted_excluded', true,
                'unclassified', (SELECT coalesce(jsonb_agg(s.value), '[]'::jsonb)
                                 FROM jsonb_array_elements(t.unclassified_snapshot) s
                                 WHERE graph.catalog_can(p_caller, (s.value ->> 'cell') || '.' || (s.value ->> 'type')
                                                                   || ':read')))
                ORDER BY t.version), '[]'::jsonb)
            FROM graph.taxonomy_versions t WHERE t.workspace_id = v_ws);
END
$$;

-- --- seed (taxonomy version 1) ----------------------------------------------------------------------

CREATE FUNCTION graph.catalog_seed() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_ws     uuid := platform.current_workspace();
    v_owner  uuid;
    v_caller jsonb;
    v_exec   uuid;
    v_row    record;
    v_none   graph.metric_definitions;
    v_count  int := 0;
BEGIN
    IF v_ws IS NULL THEN
        RAISE EXCEPTION 'catalog_seed needs a bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(graph.taxonomy_lock_key(v_ws));
    IF EXISTS (SELECT 1 FROM graph.taxonomy_versions WHERE workspace_id = v_ws) THEN
        RETURN 0;
    END IF;
    -- audit needs an active member: the recorded owner, else (ownership moved) an active owner-role member
    SELECT coalesce(
        (SELECT m.identity_id FROM platform.workspaces w
         JOIN platform.memberships m ON m.workspace_id = w.id AND m.identity_id = w.owner_identity_id
         WHERE w.id = v_ws AND m.status = 'active'),
        (SELECT m.identity_id FROM platform.memberships m
         JOIN platform.membership_roles mr USING (workspace_id, identity_id)
         JOIN platform.roles r ON r.workspace_id = mr.workspace_id AND r.id = mr.role_id
         WHERE m.workspace_id = v_ws AND m.status = 'active' AND r.name = 'owner'
         ORDER BY m.created_at, m.identity_id LIMIT 1),
        (SELECT owner_identity_id FROM platform.workspaces WHERE id = v_ws))
    INTO v_owner;
    v_caller := jsonb_build_object('actor', v_owner, 'channel', 'workflow');
    v_exec := graph.catalog_execution(v_caller);
    FOR v_row IN SELECT * FROM (VALUES
        ('domain', 'health', 'สุขภาพและพลังงาน'),
        ('domain', 'finance', 'การเงินและทรัพยากร'),
        ('domain', 'skills', 'ความรู้และทักษะ'),
        ('domain', 'work', 'งานและการสร้างคุณค่า'),
        ('domain', 'relationships', 'ความสัมพันธ์และเสน่ห์'),
        ('domain', 'experience', 'ประสบการณ์และการผจญภัย'),
        ('domain', 'aesthetics-rest', 'สุนทรียะ ความสุข และความชิล'),
        ('domain', 'meaning', 'ความหมายและการเติบโต'),
        ('domain', 'unclassified', 'ยังไม่จัดหมวด'),
        ('value', 'freedom', 'อิสรภาพ'),
        ('value', 'security', 'ความมั่นคง'),
        ('value', 'vitality', 'สุขภาพดีและมีพลัง'),
        ('value', 'love', 'ความรักและความสัมพันธ์ที่ดี'),
        ('value', 'growth', 'การเติบโต'),
        ('value', 'beauty', 'ความงามและสุนทรียะ')
    ) AS s (kind, slug, label) LOOP
        INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version)
        VALUES (graph.concept_urn(v_ws, v_row.kind, v_row.slug), v_ws, 'platform', v_row.kind, v_row.label,
                'internal', 'active', 1)
        ON CONFLICT (urn) DO NOTHING;
        INSERT INTO graph.concepts (urn, workspace_id, kind, slug, label, introduced_in)
        VALUES (graph.concept_urn(v_ws, v_row.kind, v_row.slug), v_ws, v_row.kind, v_row.slug, v_row.label, 1);
        v_count := v_count + 1;
    END LOOP;
    -- version 1 exists before revisions and metrics are written, so they record taxonomy_version 1
    PERFORM graph.taxonomy_record(v_ws, 1, 'seed: SPEC-002 domains, values and check-in metrics', v_owner);
    FOR v_row IN SELECT c.* FROM graph.concepts c WHERE c.workspace_id = v_ws ORDER BY c.kind, c.slug LOOP
        PERFORM graph.catalog_revision('concept', v_caller, v_exec, 'catalog.concept:write', 'catalog.concept',
                                       v_row.kind || '/' || v_row.slug, v_row.urn, NULL, to_jsonb(v_row));
    END LOOP;
    FOR v_row IN SELECT * FROM jsonb_each($seed$ {
        "health.sleep_hours": {
            "label": "ชั่วโมงนอน", "domain": "health", "unit": "h", "scale": "ratio",
            "description": "Hours slept in one night; one value per Bangkok day, Apple Health first.",
            "value_range": {"min": 0, "max": 24}, "kind": "leading", "objectivity": "objective",
            "source": {"cell": "health", "resource": "health-metrics", "field": "value",
                       "filter": {"metric_type": "sleep_hours"}, "time_field": "recorded_at",
                       "dedupe": {"by": "day", "tz": "Asia/Bangkok", "field": "source",
                                  "prefer": ["apple-health", "telegram-checkin"], "combine": "last"}},
            "aggregation": "mean", "cadence": "daily",
            "criteria": [{"level": "warn", "window": "7d", "op": "<", "value": 6,
                          "action": "Average sleep under 6 h this week: plan earlier nights."}],
            "target": {"window": "7d", "op": ">=", "value": 7}},
        "health.weight_kg": {
            "label": "น้ำหนัก", "domain": "health", "unit": "kg", "scale": "ratio",
            "description": "Body weight; the last reading of a Bangkok day, Apple Health first.",
            "value_range": {"min": 20, "max": 300}, "kind": "lagging", "objectivity": "objective",
            "source": {"cell": "health", "resource": "health-metrics", "field": "value",
                       "filter": {"metric_type": "weight"}, "time_field": "recorded_at",
                       "dedupe": {"by": "day", "tz": "Asia/Bangkok", "field": "source",
                                  "prefer": ["apple-health", "telegram-checkin"], "combine": "last"}},
            "aggregation": "last", "cadence": "daily", "criteria": [], "target": null},
        "health.exercise_minutes": {
            "label": "นาทีออกกำลังกาย", "domain": "health", "unit": "min", "scale": "ratio",
            "description": "Exercise minutes per Bangkok day: the Apple Health daily total, else the check-in rows summed.",
            "value_range": {"min": 0}, "kind": "leading", "objectivity": "objective",
            "source": {"cell": "health", "resource": "health-metrics", "field": "value",
                       "filter": {"metric_type": "exercise_minutes"}, "time_field": "recorded_at",
                       "dedupe": {"by": "day", "tz": "Asia/Bangkok", "field": "source",
                                  "prefer": ["apple-health", "telegram-checkin"], "combine": "sum"}},
            "aggregation": "sum", "cadence": "daily", "criteria": [],
            "target": {"window": "7d", "op": ">=", "value": 150}},
        "health.water_litres": {
            "label": "น้ำดื่ม", "domain": "health", "unit": "L", "scale": "ratio",
            "description": "Litres of water reported in the evening check-in.",
            "value_range": {"min": 0, "max": 10}, "kind": "leading", "objectivity": "subjective",
            "source": {"cell": "health", "resource": "health-metrics", "field": "value",
                       "filter": {"metric_type": "water"}, "time_field": "recorded_at"},
            "aggregation": "mean", "cadence": "daily", "criteria": [],
            "target": {"window": "7d", "op": ">=", "value": 2}},
        "diary.mood": {
            "label": "อารมณ์", "domain": "health", "unit": "score", "scale": "ordinal",
            "description": "Mood of the day, 1 = very bad, 5 = very good, asked at 21:30.",
            "value_range": {"min": 1, "max": 5}, "kind": "lagging", "objectivity": "subjective",
            "source": {"cell": "diary", "resource": "journal-entries", "field": "mood",
                       "filter": {"entry_type": "daily"}, "time_field": "entry_date"},
            "aggregation": "median", "cadence": "daily",
            "criteria": [{"level": "warn", "window": "7d", "op": "<=", "value": 2,
                          "action": "Low mood most of the week: check in with someone."}],
            "target": null},
        "diary.energy": {
            "label": "พลังงาน", "domain": "health", "unit": "score", "scale": "ordinal",
            "description": "Energy after work, 1 = empty, 5 = full, asked at 21:30.",
            "value_range": {"min": 1, "max": 5}, "kind": "guardrail", "objectivity": "subjective",
            "source": {"cell": "diary", "resource": "journal-entries", "field": "energy_level",
                       "filter": {"entry_type": "daily"}, "time_field": "entry_date"},
            "aggregation": "median", "cadence": "daily",
            "criteria": [{"level": "warn", "window": "7d", "op": "<", "value": 3, "action": "Plan rest."}],
            "target": null},
        "time.work_minutes": {
            "label": "เวลางาน", "domain": "work", "unit": "min", "scale": "ratio",
            "description": "Minutes logged with domain = work.",
            "value_range": {"min": 0}, "kind": "guardrail", "objectivity": "objective",
            "source": {"cell": "time", "resource": "time-logs", "field": "minutes",
                       "filter": {"domain": "work"}, "time_field": "log_date"},
            "aggregation": "sum", "cadence": "daily",
            "criteria": [{"level": "warn", "window": "7d", "op": ">", "value": 3600,
                          "action": "More than 60 h of work this week."}],
            "target": null},
        "time.learning_minutes": {
            "label": "เวลาเรียนรู้", "domain": "skills", "unit": "min", "scale": "ratio",
            "description": "Minutes logged with domain = learning.",
            "value_range": {"min": 0}, "kind": "leading", "objectivity": "objective",
            "source": {"cell": "time", "resource": "time-logs", "field": "minutes",
                       "filter": {"domain": "learning"}, "time_field": "log_date"},
            "aggregation": "sum", "cadence": "daily", "criteria": [], "target": null}
    } $seed$::jsonb) ORDER BY key LOOP
        PERFORM graph.store_metric(v_caller, v_exec, v_row.key, v_row.value, v_none);
    END LOOP;
    RETURN v_count;
END
$$;

-- --- ingest (D5) ------------------------------------------------------------------------------------

-- A catalog set event: the full target set for one (subject, relation, cell), ordered by set_version.
-- It never creates or updates the subject node.
CREATE FUNCTION graph.ingest_set_event(p_event jsonb, p_caller jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_id           uuid := (p_event ->> 'id')::uuid;
    v_ws           uuid := (p_event ->> 'workspace_id')::uuid;
    v_payload      jsonb := p_event -> 'payload';
    v_relation     text := p_event -> 'payload' ->> 'relation';
    v_class        text := coalesce(p_event ->> 'classification', 'internal');
    v_subject      text;
    v_parts        text[];
    v_rt           graph.relation_types;
    v_set_version  bigint;
    v_stored       bigint;
    v_lifecycle    text;
    v_ref          text;
    v_target       text;
    v_m            text[];
    v_status       text;
    v_metric       graph.metric_definitions;
    v_unclassified text;
    v_wanted       jsonb := '{}'::jsonb;  -- urn -> {"requested": [...], "direct": bool, "class": text}
    v_fallback     boolean := false;
    v_key          text;
    v_want         jsonb;
    v_prov         jsonb;
    v_edge_class   text;
    v_canonical    text;
    v_outcome      text;
BEGIN
    IF v_ws IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO graph.inbox (consumer, event_id, workspace_id, outcome)
    VALUES ('graph', v_id, v_ws, 'pending')
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        RETURN 'duplicate';
    END IF;
    v_subject := graph.resolve(p_event ->> 'subject');
    v_parts := regexp_match(v_subject, '^empire:([a-z0-9-]+):([a-z0-9-]+):');
    SELECT * INTO v_rt FROM graph.relation_types WHERE relation = v_relation;
    IF v_parts IS NULL OR NOT FOUND OR NOT v_relation = ANY (graph.catalog_relations())
       OR jsonb_typeof(v_payload -> 'targets') IS DISTINCT FROM 'array'
       OR coalesce(v_payload ->> 'set_version', '') !~ '^[0-9]{1,18}$' THEN
        v_outcome := 'invalid_event';
    ELSIF NOT ('*' = ANY (v_rt.from_types) OR v_parts[1] || ':' || v_parts[2] = ANY (v_rt.from_types)) THEN
        v_outcome := 'rejected_relation';
    END IF;
    IF v_outcome IS NOT NULL THEN
        UPDATE graph.inbox SET outcome = v_outcome WHERE consumer = 'graph' AND event_id = v_id;
        RETURN v_outcome;
    END IF;
    v_set_version := (v_payload ->> 'set_version')::bigint;

    -- step 0: one taxonomy state for the whole event; then one writer per subject (with or without a node)
    PERFORM pg_advisory_xact_lock_shared(graph.taxonomy_lock_key(v_ws));
    PERFORM pg_advisory_xact_lock(graph.subject_lock_key(v_ws, v_subject));

    SELECT lifecycle INTO v_lifecycle FROM graph.nodes WHERE urn = v_subject;
    SELECT version INTO v_stored FROM graph.edge_versions
    WHERE workspace_id = v_ws AND from_urn = v_subject AND relation = v_relation AND asserted_by = v_parts[1];
    IF v_lifecycle = 'deleted' OR v_stored >= v_set_version THEN
        UPDATE graph.inbox SET outcome = 'stale' WHERE consumer = 'graph' AND event_id = v_id;
        RETURN 'stale';
    END IF;

    -- steps 1-2: canonicalise, resolve, check every target; any rejection rejects the whole set
    v_unclassified := graph.concept_urn(v_ws, 'domain', 'unclassified');
    FOR v_ref IN SELECT jsonb_array_elements_text(v_payload -> 'targets') LOOP
        v_canonical := graph.canonical_ref(v_ref, v_ws);
        v_target := graph.resolve(v_canonical);
        v_m := regexp_match(v_target, '^empire:platform:(domain|value|metric):([0-9a-f-]{36}):(.+)$');
        IF v_m IS NULL OR v_m[2] <> v_ws::text OR NOT ('platform:' || v_m[1]) = ANY (v_rt.to_types) THEN
            v_outcome := 'rejected_relation';
            EXIT;
        END IF;
        IF v_m[1] = 'metric' THEN
            SELECT * INTO v_metric FROM graph.metric_definitions WHERE urn = v_target AND workspace_id = v_ws;
            -- protected definitions need the token's effective read permission; same answer as not found
            IF NOT FOUND OR v_metric.status <> 'active'
               OR (v_metric.classification IN ('confidential', 'restricted')
                   AND NOT graph.catalog_can(p_caller, v_metric.read_permission)) THEN
                v_outcome := 'rejected_relation';
                EXIT;
            END IF;
            v_wanted := jsonb_set(v_wanted, ARRAY[v_target],
                                  jsonb_build_object('requested', '[]'::jsonb, 'direct', true,
                                                     'class', v_metric.classification));
        ELSE
            SELECT status INTO v_status FROM graph.concepts
            WHERE urn = v_target AND workspace_id = v_ws AND kind = v_m[1];
            IF NOT FOUND OR v_status = 'merged' THEN
                v_outcome := 'rejected_relation';
                EXIT;
            ELSIF v_status = 'active' THEN
                v_want := coalesce(v_wanted -> v_target, jsonb_build_object('requested', '[]'::jsonb));
                v_wanted := jsonb_set(v_wanted, ARRAY[v_target],
                                      v_want || jsonb_build_object('direct', true, 'class', 'internal'));
            ELSIF v_relation = 'in-domain' THEN
                v_fallback := true;
                v_want := coalesce(v_wanted -> v_unclassified,
                                   jsonb_build_object('requested', '[]'::jsonb, 'direct', false, 'class', 'internal'));
                IF NOT (v_want -> 'requested') ? v_canonical THEN
                    v_want := jsonb_set(v_want, '{requested}', (v_want -> 'requested') || to_jsonb(v_canonical));
                END IF;
                v_wanted := jsonb_set(v_wanted, ARRAY[v_unclassified], v_want);
            ELSE
                v_outcome := 'rejected_relation';
                EXIT;
            END IF;
        END IF;
    END LOOP;
    IF v_outcome IS NOT NULL THEN
        UPDATE graph.inbox SET outcome = v_outcome WHERE consumer = 'graph' AND event_id = v_id;
        RETURN v_outcome;
    END IF;

    -- step 3: replace. Matching compares resolved targets, so an edge stored to A before A merged into B
    -- counts as the edge to B.
    UPDATE graph.edges SET retracted_at = now()
    WHERE workspace_id = v_ws AND from_urn = v_subject AND relation = v_relation AND asserted_by = v_parts[1]
      AND retracted_at IS NULL AND NOT v_wanted ? graph.resolve(to_urn);
    FOR v_key, v_want IN SELECT key, value FROM jsonb_each(v_wanted) LOOP
        v_prov := jsonb_build_object('event_id', v_id, 'source', 'set', 'set_version', v_set_version);
        IF jsonb_array_length(v_want -> 'requested') > 0 THEN
            v_prov := v_prov || jsonb_build_object('requested', v_want -> 'requested',
                                                   'outcome', 'fallback_unclassified',
                                                   'direct', coalesce((v_want ->> 'direct')::boolean, false));
        END IF;
        v_edge_class := CASE WHEN graph.rank(v_want ->> 'class') > graph.rank(v_class) THEN v_want ->> 'class'
                             ELSE v_class END;
        UPDATE graph.edges SET provenance = v_prov, classification = v_edge_class
        WHERE workspace_id = v_ws AND from_urn = v_subject AND relation = v_relation AND asserted_by = v_parts[1]
          AND retracted_at IS NULL AND graph.resolve(to_urn) = v_key;
        IF NOT FOUND THEN
            INSERT INTO graph.edges (workspace_id, from_urn, to_urn, relation, asserted_by, classification,
                                     provenance, execution_id)
            VALUES (v_ws, v_subject, v_key, v_relation, v_parts[1], v_edge_class,
                    v_prov, (p_event ->> 'execution_id')::uuid)
            ON CONFLICT (workspace_id, from_urn, to_urn, relation, asserted_by)
            DO UPDATE SET retracted_at = NULL, provenance = EXCLUDED.provenance,
                classification = EXCLUDED.classification, valid_from = now();
        END IF;
    END LOOP;
    INSERT INTO graph.edge_versions (workspace_id, from_urn, relation, asserted_by, version)
    VALUES (v_ws, v_subject, v_relation, v_parts[1], v_set_version)
    ON CONFLICT (workspace_id, from_urn, relation, asserted_by) DO UPDATE SET version = EXCLUDED.version;

    v_outcome := CASE WHEN v_fallback THEN 'fallback_unclassified' ELSE 'edges' END;
    UPDATE graph.inbox SET outcome = v_outcome WHERE consumer = 'graph' AND event_id = v_id;
    RETURN v_outcome;
END
$$;

-- Dispatcher with the publishing caller (for protected metric targets). The one-argument form keeps its
-- signature and grants and passes no caller.
CREATE FUNCTION graph.ingest_event(p_event jsonb, p_caller jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_id      uuid := (p_event ->> 'id')::uuid;
    v_ws      uuid := (p_event ->> 'workspace_id')::uuid;
    v_verb    text := split_part(p_event ->> 'type', '.', 3);
    v_payload jsonb := p_event -> 'payload';
    v_subject text;
    v_parts   text[];
    v_outcome text;
BEGIN
    IF v_verb = 'linked' AND v_payload ->> 'mode' = 'replace'
       AND v_payload ->> 'relation' = ANY (graph.catalog_relations()) THEN
        RETURN graph.ingest_set_event(p_event, p_caller);
    END IF;
    IF v_ws IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_subject := graph.resolve(p_event ->> 'subject');
    v_parts := regexp_match(v_subject, '^empire:([a-z0-9-]+):([a-z0-9-]+):');

    -- catalog relations change only through set events (step 4: clearing is an empty set)
    IF v_verb IN ('linked', 'unlinked') AND v_payload ->> 'relation' = ANY (graph.catalog_relations()) THEN
        INSERT INTO graph.inbox (consumer, event_id, workspace_id, outcome)
        VALUES ('graph', v_id, v_ws, 'rejected_relation')
        ON CONFLICT DO NOTHING;
        RETURN CASE WHEN FOUND THEN 'rejected_relation' ELSE 'duplicate' END;
    END IF;

    IF v_verb = 'unlinked' THEN
        INSERT INTO graph.inbox (consumer, event_id, workspace_id, outcome)
        VALUES ('graph', v_id, v_ws, 'pending')
        ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN
            RETURN 'duplicate';
        END IF;
        -- matches by resolved target: an edge stored to A before A merged into B is retracted by an unlink of B
        UPDATE graph.edges SET retracted_at = now()
        WHERE workspace_id = v_ws AND from_urn = v_subject
          AND graph.resolve(to_urn) = graph.resolve(v_payload ->> 'target_ref')
          AND relation = v_payload ->> 'relation'
          AND asserted_by = v_parts[1] AND retracted_at IS NULL;
        UPDATE graph.inbox SET outcome = 'unlinked' WHERE consumer = 'graph' AND event_id = v_id;
        RETURN 'unlinked';
    END IF;

    IF v_verb = 'deleted' OR coalesce((v_payload ->> 'deleted')::boolean, false) THEN
        -- same per-subject lock as set events, then tombstone versions for every catalog relation
        PERFORM pg_advisory_xact_lock(graph.subject_lock_key(v_ws, v_subject));
        v_outcome := graph.ingest_event_v1(p_event);
        IF v_outcome = 'deleted' AND v_parts IS NOT NULL THEN
            INSERT INTO graph.edge_versions (workspace_id, from_urn, relation, asserted_by, version)
            SELECT v_ws, v_subject, r, v_parts[1], coalesce((v_payload ->> 'version')::bigint, 0)
            FROM unnest(graph.catalog_relations()) r
            ON CONFLICT (workspace_id, from_urn, relation, asserted_by)
            DO UPDATE SET version = greatest(graph.edge_versions.version, EXCLUDED.version);
        END IF;
        RETURN v_outcome;
    END IF;

    RETURN graph.ingest_event_v1(p_event);
END
$$;

CREATE OR REPLACE FUNCTION graph.ingest_event(p_event jsonb) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    RETURN graph.ingest_event(p_event, NULL::jsonb);
END
$$;

-- HTTP ingest with the publishing caller ({"actor", "delegation", "scopes"} of the verified token).
CREATE FUNCTION graph.ingest_cell_event(p_event jsonb, p_cell text, p_caller jsonb) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_outcome text;
    v_subject text;
BEGIN
    IF p_cell IS NULL OR p_cell !~ '^[a-z][a-z0-9-]*$' THEN
        RAISE EXCEPTION 'invalid cell' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_event ->> 'workspace_id')::uuid IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF split_part(p_event ->> 'type', '.', 1) IS DISTINCT FROM p_cell THEN
        RAISE EXCEPTION 'event type outside the cell' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_subject := graph.resolve(p_event ->> 'subject');
    IF v_subject IS NULL OR split_part(v_subject, ':', 2) IS DISTINCT FROM p_cell
       OR split_part(p_event ->> 'subject', ':', 2) IS DISTINCT FROM p_cell THEN
        RAISE EXCEPTION 'subject outside the cell' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_outcome := graph.ingest_event(p_event, p_caller);
    IF v_outcome <> 'duplicate' THEN
        PERFORM graph.record_correlation(p_event);
    END IF;
    RETURN v_outcome;
END
$$;

CREATE OR REPLACE FUNCTION graph.ingest_cell_event(p_event jsonb, p_cell text) RETURNS text
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT graph.ingest_cell_event(p_event, p_cell, NULL::jsonb)
$$;

-- --- privileges (D8) ----------------------------------------------------------------------------------

DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'graph' AND p.proname IN (
                 'catalog_relations', 'reject_revision_change', 'catalog_fail', 'concept_urn', 'canonical_ref',
                 'taxonomy_lock_key', 'subject_lock_key', 'caller_scopes', 'catalog_can', 'catalog_workspace',
                 'catalog_authorize', 'catalog_execution', 'catalog_hash', 'catalog_revision',
                 'unclassified_snapshot', 'taxonomy_next', 'taxonomy_record', 'concept_json', 'metric_json',
                 'metric_readable', 'find_concept', 'check_parent', 'aggregation_allowed', 'json_number',
                 'check_threshold', 'store_metric', 'move_metric', 'apply_metric_moves', 'check_retirable',
                 'catalog_list_concepts', 'catalog_create_concept', 'catalog_update_concept', 'catalog_merge',
                 'catalog_split', 'catalog_retire', 'catalog_list_metrics', 'catalog_get_metric',
                 'catalog_put_metric', 'catalog_review', 'catalog_versions', 'catalog_seed', 'ingest_set_event')
                OR p.oid = 'graph.ingest_event(jsonb, jsonb)'::regprocedure
                OR p.oid = 'graph.ingest_cell_event(jsonb, text, jsonb)'::regprocedure LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    END LOOP;
END
$$;

GRANT EXECUTE ON FUNCTION
    graph.catalog_list_concepts(jsonb, text, text),
    graph.catalog_create_concept(jsonb, text, text, text, text, text, text),
    graph.catalog_update_concept(jsonb, text, text, jsonb),
    graph.catalog_merge(jsonb, text, text, text, text),
    graph.catalog_split(jsonb, text, text, text[], text, jsonb),
    graph.catalog_retire(jsonb, text, text, text, jsonb),
    graph.catalog_list_metrics(jsonb, text, text),
    graph.catalog_get_metric(jsonb, text),
    graph.catalog_put_metric(jsonb, text, jsonb, int),
    graph.catalog_review(jsonb),
    graph.catalog_versions(jsonb),
    graph.ingest_cell_event(jsonb, text, jsonb)
TO tasks_api;

-- graph_api applies events directly (consumer) and will serve the read API: the ingest path and SELECT.
GRANT SELECT ON graph.concepts, graph.metric_definitions, graph.metric_sources, graph.taxonomy_versions
    TO graph_api;
GRANT SELECT, INSERT, UPDATE ON graph.edge_versions TO graph_api;
GRANT EXECUTE ON FUNCTION
    graph.ingest_event(jsonb, jsonb), graph.ingest_set_event(jsonb, jsonb), graph.catalog_relations(),
    graph.concept_urn(uuid, text, text), graph.canonical_ref(text, uuid), graph.taxonomy_lock_key(uuid),
    graph.subject_lock_key(uuid, text), graph.caller_scopes(jsonb), graph.catalog_can(jsonb, text)
TO graph_api;

GRANT USAGE ON SCHEMA graph TO platform_tenancy;
GRANT EXECUTE ON FUNCTION graph.catalog_seed() TO platform_tenancy;

-- --- owner permissions and seed for existing workspaces ---------------------------------------------
-- platform.workspaces has FORCE RLS, so the unbound migrator sees no rows (0011). FORCE is lifted for the
-- one read inside this migration's transaction, then each workspace is bound and seeded.

DO $$
DECLARE
    v_ids uuid[];
    w     uuid;
BEGIN
    ALTER TABLE platform.workspaces NO FORCE ROW LEVEL SECURITY;
    SELECT coalesce(array_agg(id ORDER BY id), '{}') INTO v_ids FROM platform.workspaces;
    ALTER TABLE platform.workspaces FORCE ROW LEVEL SECURITY;
    FOREACH w IN ARRAY v_ids LOOP
        PERFORM set_config('app.workspace_id', w::text, true);
        INSERT INTO platform.role_permissions (workspace_id, role_id, permission)
        SELECT w, r.id, p::platform.permission
        FROM platform.roles r,
             unnest(ARRAY['catalog.concept:read', 'catalog.concept:write', 'catalog.concept:admin',
                          'catalog.metric-definition:read', 'catalog.metric-definition:write']) p
        WHERE r.workspace_id = w AND r.name = 'owner'
        ON CONFLICT DO NOTHING;
        PERFORM graph.catalog_seed();
    END LOOP;
    PERFORM set_config('app.workspace_id', '', true);
END
$$;

-- A signup already running the old tenancy_signup can wait on this migration's locks and finish after it
-- commits, creating a workspace the backfill above never saw. Every signup inserts platform.signups, so a
-- trigger there grants and seeds whichever function version ran (idempotent with the new function).
CREATE FUNCTION graph.catalog_seed_signup() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM set_config('app.workspace_id', NEW.workspace_id::text, true);
    INSERT INTO platform.role_permissions (workspace_id, role_id, permission)
    SELECT NEW.workspace_id, r.id, p::platform.permission
    FROM platform.roles r,
         unnest(ARRAY['catalog.concept:read', 'catalog.concept:write', 'catalog.concept:admin',
                      'catalog.metric-definition:read', 'catalog.metric-definition:write']) p
    WHERE r.workspace_id = NEW.workspace_id AND r.name = 'owner'
    ON CONFLICT DO NOTHING;
    PERFORM graph.catalog_seed();
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION graph.catalog_seed_signup() FROM PUBLIC;
CREATE TRIGGER signups_catalog_seed AFTER INSERT ON platform.signups
    FOR EACH ROW EXECUTE FUNCTION graph.catalog_seed_signup();

-- --- new workspaces: tenancy_signup grants the catalog permissions and seeds (D8, Seed) ---------------
-- Same hand-over as 0010: the function belongs to platform_tenancy, so it is replaced as that role.

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
    -- SPEC-002: owners manage the catalog whatever role map identity sent; then seed taxonomy version 1.
    INSERT INTO platform.role_permissions (workspace_id, role_id, permission)
    SELECT v_ws, v_owner, p::platform.permission
    FROM unnest(ARRAY['catalog.concept:read', 'catalog.concept:write', 'catalog.concept:admin',
                      'catalog.metric-definition:read', 'catalog.metric-definition:write']) AS p
    ON CONFLICT DO NOTHING;
    INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (v_ws, v_identity, v_owner);
    PERFORM graph.catalog_seed();
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
