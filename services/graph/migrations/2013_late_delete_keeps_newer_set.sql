-- SPEC-002 with re-imported subjects (platform-cellkit 1.1.12+, after 2012): a legacy id imported again after a delete
-- emits .created and its in-domain set above the tombstone. If the old incarnation's .deleted reaches the graph after
-- that set (a retried or parallel publish), the delete retracted every edge of the subject, including the newer set's,
-- and edge_versions already held the newer set_version, so the set could never be applied again: the link was lost.
--
-- The delete path now puts back the catalog edges it just retracted when their relation holds a set_version above
-- the deleted version. Only that step is new; the rest of the function is 2009's body.

CREATE OR REPLACE FUNCTION graph.ingest_event(p_event jsonb, p_caller jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_id      uuid := (p_event ->> 'id')::uuid;
    v_ws      uuid := (p_event ->> 'workspace_id')::uuid;
    v_verb    text := split_part(p_event ->> 'type', '.', 3);
    v_payload jsonb := p_event -> 'payload';
    v_subject text;
    v_parts   text[];
    v_outcome text;
    v_deleted bigint;
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
        v_deleted := coalesce((v_payload ->> 'version')::bigint, 0);
        v_outcome := graph.ingest_event_v1(p_event);
        IF v_outcome = 'deleted' AND v_parts IS NOT NULL THEN
            -- 2013: a late delete of an earlier incarnation keeps the links a newer set (a re-import above the
            -- tombstone) already stored: put back the catalog edges this delete just retracted whose relation
            -- holds a higher set_version. The node stays deleted until the re-import's .created revives it.
            UPDATE graph.edges e SET retracted_at = NULL
            WHERE e.workspace_id = v_ws AND e.from_urn = v_subject AND e.asserted_by = v_parts[1]
              AND e.relation = ANY (graph.catalog_relations()) AND e.retracted_at = now()
              AND EXISTS (SELECT 1 FROM graph.edge_versions ev
                          WHERE ev.workspace_id = v_ws AND ev.from_urn = v_subject AND ev.relation = e.relation
                            AND ev.asserted_by = v_parts[1] AND ev.version > v_deleted);
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
