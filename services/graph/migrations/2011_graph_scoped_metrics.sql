-- SPEC-002 D7b on the graph read API: a scoped token (the MCP adapter, token exchange) may see a confidential or
-- restricted metric definition node only when the source's read_permission is also in the token's scopes, as the
-- catalog API already requires. Before this the graph checked only the identity's permissions, so a token holding
-- just graph.query:read showed protected metric labels and URNs to an identity that may read their source.
--
-- node_visible, neighbors and search gain a p_scopes text[] overload (NULL = an unscoped caller, unchanged); the
-- existing signatures delegate with NULL, so catalog_review and the 2001/2006 callers keep their behaviour. The
-- graph service passes the verified token's scopes. Only protected metric nodes change: other nodes keep the
-- identity check (scoped graph tokens carry only graph.query:read by design, see token_exchange.AUDIENCE_SCOPES).

CREATE FUNCTION graph.node_visible(p_urn text, p_actor uuid, p_delegation uuid, p_scopes text[]) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM graph.nodes n
        WHERE n.urn = p_urn AND n.lifecycle <> 'deleted'
          AND platform.can(p_actor,
                           CASE WHEN n.cell = 'platform' AND n.type IN ('domain', 'value') THEN 'catalog.concept:read'
                                WHEN n.cell = 'platform' AND n.type = 'metric' THEN 'catalog.metric-definition:read'
                                ELSE n.cell || '.' || n.type || ':read' END,
                           p_delegation)
          AND (n.cell <> 'platform' OR n.type <> 'metric' OR n.classification IN ('public', 'internal')
               OR EXISTS (SELECT 1 FROM graph.metric_definitions m
                          WHERE m.urn = n.urn
                            AND platform.can(p_actor, m.read_permission, p_delegation)
                            AND (p_scopes IS NULL OR m.read_permission = ANY (p_scopes))))
    )
$$;

CREATE OR REPLACE FUNCTION graph.node_visible(p_urn text, p_actor uuid, p_delegation uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT graph.node_visible(p_urn, p_actor, p_delegation, NULL::text[])
$$;

CREATE FUNCTION graph.neighbors(p_urn text, p_actor uuid, p_delegation uuid, p_limit int, p_scopes text[])
RETURNS TABLE (urn text, cell text, type text, label text, classification text, relation text, direction text)
LANGUAGE sql STABLE AS $$
    WITH start AS (SELECT graph.resolve(p_urn) AS urn),
    names AS (
        SELECT s.urn AS name FROM start s
        UNION SELECT a.alias_urn FROM graph.aliases a JOIN start s ON a.urn = s.urn
    ),
    hits AS (
        SELECT e.relation, e.classification AS edge_class, e.from_urn, e.to_urn,
               CASE WHEN e.from_urn IN (SELECT name FROM names) THEN graph.resolve(e.to_urn)
                    ELSE graph.resolve(e.from_urn) END AS other,
               CASE WHEN e.from_urn IN (SELECT name FROM names) THEN 'out' ELSE 'in' END AS direction
        FROM graph.edges e
        WHERE e.retracted_at IS NULL
          AND (e.from_urn IN (SELECT name FROM names) OR e.to_urn IN (SELECT name FROM names))
    )
    SELECT DISTINCT n.urn, n.cell, n.type, n.label, n.classification, h.relation, h.direction
    FROM start s
    JOIN hits h ON true
    JOIN graph.nodes n ON n.urn = h.other
    WHERE graph.node_visible(s.urn, p_actor, p_delegation, p_scopes)
      AND graph.node_visible(h.other, p_actor, p_delegation, p_scopes)
      AND (h.edge_class <> 'restricted' OR EXISTS (
           SELECT 1 FROM graph.consents c
           WHERE c.relation = h.relation AND c.subject_urn IN (h.from_urn, h.to_urn)  -- stored endpoints only, as edge_visible
             AND c.revoked_at IS NULL AND c.expires_at > now()))
    ORDER BY h.relation, n.urn
    LIMIT least(greatest(p_limit, 1), 500)
$$;

CREATE OR REPLACE FUNCTION graph.neighbors(p_urn text, p_actor uuid, p_delegation uuid DEFAULT NULL, p_limit int DEFAULT 100)
RETURNS TABLE (urn text, cell text, type text, label text, classification text, relation text, direction text)
LANGUAGE sql STABLE AS $$
    SELECT * FROM graph.neighbors(p_urn, p_actor, p_delegation, p_limit, NULL::text[])
$$;

CREATE FUNCTION graph.search(p_query text, p_actor uuid, p_delegation uuid, p_limit int, p_scopes text[])
RETURNS TABLE (urn text, cell text, type text, label text, classification text)
LANGUAGE sql STABLE AS $$
    SELECT n.urn, n.cell, n.type, n.label, n.classification
    FROM graph.nodes n
    WHERE n.label ILIKE '%' || replace(replace(p_query, '%', '\%'), '_', '\_') || '%'
      AND graph.node_visible(n.urn, p_actor, p_delegation, p_scopes)
    ORDER BY n.label, n.urn
    LIMIT least(greatest(p_limit, 1), 200)
$$;

CREATE OR REPLACE FUNCTION graph.search(p_query text, p_actor uuid, p_delegation uuid DEFAULT NULL, p_limit int DEFAULT 50)
RETURNS TABLE (urn text, cell text, type text, label text, classification text)
LANGUAGE sql STABLE AS $$
    SELECT * FROM graph.search(p_query, p_actor, p_delegation, p_limit, NULL::text[])
$$;
