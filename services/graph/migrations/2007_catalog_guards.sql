-- SPEC-002 follow-up to 2006_catalog (already applied, so fixed forward here).
--
-- 1. D7: `dedupe.field` must be the register's writer field. 2006 only compared the two when the register named
--    one, so a source without a writer field (time-logs, journal-entries: one writer each) accepted any
--    `dedupe`. Refuse it on every write of a definition.
-- 2. D7b / consent: an edge's classification is fixed when the edge is written (the higher of the subject's and
--    the target's). When `PUT` raises a catalog node (a metric) above its edges, those edges kept the old class,
--    so a raised-to-restricted metric's existing links stayed visible without consent while new links needed
--    it. Raise the node's edges with it in the same transaction. Lowering a node leaves its edges as they are
--    (the stricter class stays until the next set event rewrites the edge).

CREATE FUNCTION graph.metric_dedupe_guard() RETURNS trigger
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
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION graph.metric_dedupe_guard() FROM PUBLIC;

CREATE TRIGGER metric_definitions_dedupe_guard
    BEFORE INSERT OR UPDATE OF source ON graph.metric_definitions
    FOR EACH ROW EXECUTE FUNCTION graph.metric_dedupe_guard();

CREATE FUNCTION graph.catalog_node_raise_edges() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE graph.edges e SET classification = NEW.classification
    WHERE e.workspace_id = NEW.workspace_id
      AND (e.from_urn = NEW.urn OR e.to_urn = NEW.urn)
      AND graph.rank(e.classification) < graph.rank(NEW.classification);
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION graph.catalog_node_raise_edges() FROM PUBLIC;

CREATE TRIGGER nodes_catalog_raise_edges
    AFTER UPDATE OF classification ON graph.nodes
    FOR EACH ROW
    WHEN (NEW.cell = 'platform' AND graph.rank(NEW.classification) > graph.rank(OLD.classification))
    EXECUTE FUNCTION graph.catalog_node_raise_edges();
