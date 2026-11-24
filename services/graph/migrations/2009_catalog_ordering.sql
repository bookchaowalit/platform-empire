-- SPEC-002 follow-up to 2006-2008 (all applied on hosted, so fixed forward here).
--
-- 1. D4 / AC-41 (set event before the subject's lifecycle event): the 2008 edge floor found no subject node,
--    and 2008's raise trigger fired on UPDATE only, so the node's first INSERT as `restricted` left an
--    `internal` catalog edge. The raise now also runs when a node is created. Both raises take the subject's
--    advisory lock (the one the set ingest holds), so a lifecycle event and a set event written at the same
--    time cannot each miss the other's row.
-- 2. D4 snapshot: an in-domain edge to unclassified whose subject has no node yet (or a deleted one) is not
--    counted; its class is not known until the node arrives, and a stored count is never corrected.
-- 4. Tenancy: node URNs carry no workspace, so every node lookup here (and the 2008 edge floor) only reads
--    nodes of the edge's own workspace; another workspace's node never sets this workspace's classes.
-- 3. D7: `dedupe.combine` folds the writers of one day before the aggregate, so it must be an aggregation the
--    metric's scale allows (no `sum` of ordinal scores). Checked with the same rule as `aggregation`.

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
