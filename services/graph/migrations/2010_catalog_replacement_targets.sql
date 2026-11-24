-- SPEC-002 follow-up to 2006-2009 (all applied on hosted, so fixed forward here).
--
-- D2: `replaced_by` targets must be active. 2006 checked that only when the split was written, so a split target could
-- later leave `active` and break its referrer: after `split A -> {B, C}`, merging B into C collapsed A's two targets
-- into one while A stayed `split`, and retiring or splitting B left A offering an unusable target for reclassifying.
-- A concept listed by a `split` concept now stays active (409 `replacement_target` naming the referrers).
-- Merged referrers keep 2006-2008 behaviour: the alias chain follows the target through a later merge, split or
-- retire, and review falls back as for any non-active concept (test_review_counts_each_entity_once_...).

CREATE FUNCTION graph.concept_replacement_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_refs text[];
BEGIN
    SELECT array_agg(c.slug ORDER BY c.slug) INTO v_refs
    FROM graph.concepts c
    WHERE c.workspace_id = OLD.workspace_id AND c.status = 'split' AND OLD.urn = ANY (c.replaced_by);
    IF v_refs IS NOT NULL THEN
        PERFORM graph.catalog_fail(409, 'replacement_target', jsonb_build_object('replaces', to_jsonb(v_refs)));
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION graph.concept_replacement_guard() FROM PUBLIC;

CREATE TRIGGER concepts_replacement_guard
    BEFORE UPDATE OF status ON graph.concepts
    FOR EACH ROW
    WHEN (OLD.status = 'active' AND NEW.status <> 'active')
    EXECUTE FUNCTION graph.concept_replacement_guard();
