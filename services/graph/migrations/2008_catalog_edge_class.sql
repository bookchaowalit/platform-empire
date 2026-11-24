-- SPEC-002 follow-up to 2006/2007 (both applied on hosted, so fixed forward here).
--
-- 1. D4 / consent: a catalog set event classed its edges from the event and the target only. A subject stored
--    as `restricted` (its lifecycle event) whose set event said `internal` (or nothing) got an `internal` edge,
--    so `/v1/catalog/review` counted it without consent and a version snapshot kept that count. A catalog edge
--    is now never below either endpoint's stored class, on every write.
-- 2. The 2007 trigger raised edges only when a platform catalog node was raised. A later lifecycle event that
--    raises the subject (a goal, a time log) now raises its catalog edges too. Other relations keep 2001's
--    behaviour, except edges of platform catalog nodes (2007).
-- 3. D2-D3: after merges A -> B and B -> C, A's `replaced_by` still named B (merged, not active). Concept JSON
--    now resolves each replacement through the aliases, so it names the current target C.

CREATE FUNCTION graph.catalog_edge_floor() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_class text;
BEGIN
    FOR v_class IN SELECT n.classification FROM graph.nodes n WHERE n.urn IN (NEW.from_urn, NEW.to_urn) LOOP
        IF graph.rank(v_class) > graph.rank(NEW.classification) THEN
            NEW.classification := v_class;
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION graph.catalog_edge_floor() FROM PUBLIC;

CREATE TRIGGER edges_catalog_floor
    BEFORE INSERT OR UPDATE ON graph.edges
    FOR EACH ROW
    WHEN (NEW.relation = ANY (graph.catalog_relations()))
    EXECUTE FUNCTION graph.catalog_edge_floor();

CREATE OR REPLACE FUNCTION graph.catalog_node_raise_edges() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE graph.edges e SET classification = NEW.classification
    WHERE e.workspace_id = NEW.workspace_id
      AND (e.from_urn = NEW.urn OR e.to_urn = NEW.urn)
      AND (NEW.cell = 'platform' OR e.relation = ANY (graph.catalog_relations()))
      AND graph.rank(e.classification) < graph.rank(NEW.classification);
    RETURN NULL;
END
$$;

DROP TRIGGER nodes_catalog_raise_edges ON graph.nodes;
CREATE TRIGGER nodes_catalog_raise_edges
    AFTER UPDATE OF classification ON graph.nodes
    FOR EACH ROW
    WHEN (graph.rank(NEW.classification) > graph.rank(OLD.classification))
    EXECUTE FUNCTION graph.catalog_node_raise_edges();

CREATE OR REPLACE FUNCTION graph.concept_json(p_c graph.concepts) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'urn', p_c.urn, 'kind', p_c.kind, 'slug', p_c.slug, 'label', p_c.label, 'description', p_c.description,
        'parent', (SELECT slug FROM graph.concepts WHERE urn = p_c.parent_urn),
        'status', p_c.status,
        'replaced_by', (SELECT coalesce(jsonb_agg(jsonb_build_object('kind', r.kind, 'slug', r.slug, 'urn', r.urn)
                                                  ORDER BY r.slug), '[]'::jsonb)
                        FROM graph.concepts r
                        WHERE r.urn IN (SELECT graph.resolve(x) FROM unnest(p_c.replaced_by) x)),
        'introduced_in', p_c.introduced_in, 'retired_in', p_c.retired_in,
        'was', (SELECT coalesce(jsonb_agg(a.alias_urn ORDER BY a.alias_urn), '[]'::jsonb)
                FROM graph.aliases a WHERE a.urn = p_c.urn),
        'updated_at', p_c.updated_at)
$$;
