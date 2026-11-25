-- SPEC-002 D5 with re-imported subjects: a legacy id imported again after a delete (platform-cellkit 1.1.12 starts it
-- above the tombstone) emits .created and its in-domain set at the same new version. Publishers sweep the outbox in
-- parallel (SKIP LOCKED), so the set can reach the graph before the .created that revives the node; 2006 then
-- recorded it `stale` because the node was still deleted, and the re-imported row lost its link for good.
--
-- A set event for a deleted subject is now `stale` only when its set_version is at or below the node's (tombstone)
-- version. A delayed set of the deleted incarnation is still refused (its version is below the tombstone, also via
-- edge_versions); a higher one is stored, hidden while the node is deleted, and live once the .created arrives.
-- Only the staleness test changes; the rest of the function is 2006's body.

CREATE OR REPLACE FUNCTION graph.ingest_set_event(p_event jsonb, p_caller jsonb) RETURNS text LANGUAGE plpgsql AS $$
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
    v_node_version bigint;
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
    IF v_parts IS NULL OR v_subject !~ '^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$' OR NOT FOUND
       OR NOT v_relation = ANY (graph.catalog_relations())
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

    SELECT lifecycle, version INTO v_lifecycle, v_node_version FROM graph.nodes WHERE urn = v_subject;
    SELECT version INTO v_stored FROM graph.edge_versions
    WHERE workspace_id = v_ws AND from_urn = v_subject AND relation = v_relation AND asserted_by = v_parts[1];
    -- 2012: a deleted subject refuses only sets at or below its tombstone; a higher set_version is a later
    -- incarnation (a legacy id re-imported, platform-cellkit 1.1.12) whose set may arrive before its .created
    IF (v_lifecycle = 'deleted' AND v_set_version <= coalesce(v_node_version, 0)) OR v_stored >= v_set_version THEN
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
        UPDATE graph.edges SET provenance = v_prov, classification = v_edge_class,
            execution_id = (p_event ->> 'execution_id')::uuid
        WHERE workspace_id = v_ws AND from_urn = v_subject AND relation = v_relation AND asserted_by = v_parts[1]
          AND retracted_at IS NULL AND graph.resolve(to_urn) = v_key;
        IF NOT FOUND THEN
            INSERT INTO graph.edges (workspace_id, from_urn, to_urn, relation, asserted_by, classification,
                                     provenance, execution_id)
            VALUES (v_ws, v_subject, v_key, v_relation, v_parts[1], v_edge_class,
                    v_prov, (p_event ->> 'execution_id')::uuid)
            ON CONFLICT (workspace_id, from_urn, to_urn, relation, asserted_by)
            DO UPDATE SET retracted_at = NULL, provenance = EXCLUDED.provenance,
                classification = EXCLUDED.classification, execution_id = EXCLUDED.execution_id, valid_from = now();
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
