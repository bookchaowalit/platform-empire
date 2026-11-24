-- SPEC-002 follow-up to 2006-2008 (all applied on hosted, so fixed forward here).
--
-- 1. D4 / AC-41 (set event before the subject's lifecycle event): the 2008 edge floor found no subject node,
--    and 2008's raise trigger fired on UPDATE only, so the node's first INSERT as `restricted` left an
--    `internal` catalog edge. The raise now also runs when a node is created. Both raises take the subject's
--    advisory lock (the one the set ingest holds), so a lifecycle event and a set event written at the same
--    time cannot each miss the other's row.
-- 2. D4 snapshot: an in-domain edge to unclassified whose subject has no node yet (or a deleted one) is not
--    counted; its class is not known until the node arrives, and a stored count is never corrected.
-- 3. D7: `dedupe.combine` folds the writers of one day before the aggregate, so it must be an aggregation the
--    metric's scale allows (no `sum` of ordinal scores). Checked with the same rule as `aggregation`.
-- 4. Tenancy: node URNs carry no workspace, so every node lookup here (and the 2008 edge floor) only reads
--    nodes of the edge's own workspace; another workspace's node never sets this workspace's classes.
-- 5. Lock order: lifecycle events now take the subject lock before writing the node, as set and delete events
--    do, so the raise trigger (which takes it after the node row) cannot deadlock with a delete.
-- 6. Repair: catalog edges written under 2008 before their subject existed are raised once to the floor.

CREATE OR REPLACE FUNCTION graph.catalog_node_raise_edges() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(graph.subject_lock_key(NEW.workspace_id, NEW.urn));
    UPDATE graph.edges e SET classification = NEW.classification
    WHERE e.workspace_id = NEW.workspace_id
      AND (e.from_urn = NEW.urn OR e.to_urn = NEW.urn)
      AND (NEW.cell = 'platform' OR e.relation = ANY (graph.catalog_relations()))
      AND graph.rank(e.classification) < graph.rank(NEW.classification);
    RETURN NULL;
END
$$;

CREATE TRIGGER nodes_catalog_raise_edges_on_create
    AFTER INSERT ON graph.nodes
    FOR EACH ROW
    WHEN (NEW.classification <> 'public')
    EXECUTE FUNCTION graph.catalog_node_raise_edges();

CREATE OR REPLACE FUNCTION graph.unclassified_snapshot(p_ws uuid) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('cell', cell, 'type', type, 'classification', classification,
                                                 'count', n) ORDER BY cell, type, classification), '[]'::jsonb)
    FROM (
        SELECT split_part(from_urn, ':', 2) AS cell, split_part(from_urn, ':', 3) AS type, classification,
               count(*) AS n
        FROM (
            SELECT DISTINCT ON (e.from_urn) e.from_urn,
                   CASE WHEN graph.rank(n.classification) > graph.rank(e.classification) THEN n.classification
                        ELSE e.classification END AS classification
            FROM graph.edges e
            JOIN graph.nodes n ON n.urn = e.from_urn AND n.workspace_id = p_ws AND n.lifecycle <> 'deleted'
            WHERE e.workspace_id = p_ws AND e.relation = 'in-domain' AND e.retracted_at IS NULL
              AND graph.resolve(e.to_urn) = graph.concept_urn(p_ws, 'domain', 'unclassified')
            ORDER BY e.from_urn, graph.rank(e.classification) DESC
        ) entity
        WHERE classification <> 'restricted'
        GROUP BY 1, 2, 3
    ) s
$$;

CREATE OR REPLACE FUNCTION graph.metric_dedupe_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_writer text;
BEGIN
    IF NEW.source -> 'dedupe' IS NULL OR jsonb_typeof(NEW.source -> 'dedupe') = 'null' THEN
        RETURN NEW;
    END IF;
    SELECT s.writer_field INTO v_writer FROM graph.metric_sources s
    WHERE s.cell = NEW.source ->> 'cell' AND s.resource = NEW.source ->> 'resource';
    IF v_writer IS NULL OR NEW.source -> 'dedupe' ->> 'field' IS DISTINCT FROM v_writer THEN
        PERFORM graph.catalog_fail(422, 'dedupe_field_not_writer', jsonb_build_object('writer_field', v_writer));
    END IF;
    IF NOT graph.aggregation_allowed(NEW.scale, NEW.source -> 'dedupe' ->> 'combine') THEN
        PERFORM graph.catalog_fail(422, 'combine_not_allowed',
                                   jsonb_build_object('scale', NEW.scale,
                                                      'combine', NEW.source -> 'dedupe' ->> 'combine'));
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER metric_definitions_dedupe_guard ON graph.metric_definitions;
CREATE TRIGGER metric_definitions_dedupe_guard
    BEFORE INSERT OR UPDATE OF source, scale ON graph.metric_definitions
    FOR EACH ROW EXECUTE FUNCTION graph.metric_dedupe_guard();

CREATE OR REPLACE FUNCTION graph.catalog_edge_floor() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_class text;
BEGIN
    FOR v_class IN SELECT n.classification FROM graph.nodes n
                   WHERE n.workspace_id = NEW.workspace_id AND n.urn IN (NEW.from_urn, NEW.to_urn) LOOP
        IF graph.rank(v_class) > graph.rank(NEW.classification) THEN
            NEW.classification := v_class;
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION graph.ingest_event(p_event jsonb, p_caller jsonb) RETURNS text LANGUAGE plpgsql AS $$
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

    -- 2009: lifecycle writes take the subject lock before touching the node, like set and delete events,
    -- so every path locks subject -> node row (no deadlock with the raise trigger)
    IF v_parts IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(graph.subject_lock_key(v_ws, v_subject));
    END IF;
    RETURN graph.ingest_event_v1(p_event);
END
$$;

-- Repair existing catalog edges once (the edges_catalog_floor trigger computes the class). Both tables FORCE RLS
-- and the migrator binds no workspace, so lift FORCE for this statement only (same transaction). Locks are
-- taken nodes first, then edges: the order every ingest path uses, so a concurrent event waits, never deadlocks.
LOCK TABLE graph.nodes, graph.edges IN ACCESS EXCLUSIVE MODE;
ALTER TABLE graph.nodes NO FORCE ROW LEVEL SECURITY;
ALTER TABLE graph.edges NO FORCE ROW LEVEL SECURITY;
UPDATE graph.edges SET classification = classification WHERE relation = ANY (graph.catalog_relations());
ALTER TABLE graph.edges FORCE ROW LEVEL SECURITY;
ALTER TABLE graph.nodes FORCE ROW LEVEL SECURITY;
