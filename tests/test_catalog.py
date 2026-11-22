"""SPEC-002 catalog (migration 2006): domains, values, metric definitions and the catalog ingest.

Entry points are called as tasks_api, the role the catalog routes run on; each call is its own committed
transaction bound to one workspace, like a route. AC numbers refer to the spec's acceptance list.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import psycopg
import pytest
from conftest import (
    Workspace,
    add_member,
    create_workspace,
    delegate,
    new_id,
    use_workspace,
)
from psycopg.types.json import Jsonb

from empire_platform import devdb

CATALOG = [
    "catalog.concept:read",
    "catalog.concept:write",
    "catalog.concept:admin",
    "catalog.metric-definition:read",
    "catalog.metric-definition:write",
]
READS = [
    "goals.goal:read",
    "goals.goal:write",
    "time.time-log:read",
    "diary.journal-entry:read",
    "health.health-metric:read",
    "tasks.task:read",
    "operating.kpi:read",
]


class CatalogError(Exception):
    def __init__(self, status: int, code: str, detail: dict[str, Any]):
        super().__init__(f"{status} {code} {detail}")
        self.status, self.code, self.detail = status, code, detail


def su(cluster, query: str, params: tuple = ()) -> list[tuple]:
    with cluster.connect("postgres", "empire") as conn:
        cur = conn.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows


def cat(cluster, ws: uuid.UUID, fn: str, *args: Any) -> Any:
    """SELECT graph.<fn>(...) as tasks_api in one transaction bound to ws; CT<status> errors become CatalogError."""
    placeholders = ", ".join(["%s"] * len(args))
    with cluster.connect("tasks_api") as conn:
        use_workspace(conn, ws)
        try:
            row = conn.execute(
                f"SELECT graph.{fn}({placeholders})",  # nosemgrep: fixed function names in tests
                [Jsonb(a) if isinstance(a, dict) else a for a in args],
            ).fetchone()
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            if exc.sqlstate and exc.sqlstate.startswith("CT"):
                raise CatalogError(
                    int(exc.sqlstate[2:]),
                    exc.diag.message_primary or "",
                    json.loads(exc.diag.message_detail or "{}"),
                ) from None
            raise
    result = row[0] if row else None
    # refused writes come back as a value so the authorization decision row commits
    if (
        isinstance(result, dict)
        and isinstance(result.get("status"), int)
        and "error" in result
    ):
        raise CatalogError(
            result["status"], result["error"], {"reason": result.get("reason")}
        )
    return result


def caller(
    actor: uuid.UUID, scopes: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "actor": str(actor),
        **{k: str(v) for k, v in extra.items()},
    }
    if scopes is not None:
        body["scopes"] = scopes
    return body


def add_role(
    conn, ws: Workspace, identity: uuid.UUID, name: str, perms: list[str]
) -> None:
    role = new_id(conn)
    use_workspace(conn, ws.id)
    conn.execute(
        "INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, %s)",
        (role, ws.id, name),
    )
    for perm in perms:
        conn.execute(
            "INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
            (ws.id, role, perm),
        )
    conn.execute(
        "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
        (ws.id, identity, role),
    )


def seed(cluster, ws: uuid.UUID) -> int:
    with cluster.connect("empire_migrator") as conn:
        use_workspace(conn, ws)
        count = conn.execute("SELECT graph.catalog_seed()").fetchone()[0]
        conn.commit()
        return count


def seeded(cluster, perms: list[str] | None = None) -> Workspace:
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"cat-{uuid.uuid4().hex[:8]}", perms or CATALOG + READS
        )
        conn.commit()
    assert seed(cluster, ws.id) == 15
    return ws


def urn(ws: Workspace, kind: str, slug: str) -> str:
    return f"empire:platform:{kind}:{ws.id}:{slug}"


def lifecycle(
    ws,
    cell,
    type_,
    ident,
    verb="created",
    version=1,
    title="Item",
    classification="internal",
):
    return {
        "id": str(uuid.uuid4()),
        "workspace_id": str(ws.id),
        "type": f"{cell}.{type_}.{verb}",
        "version": 1,
        "subject": f"empire:{cell}:{type_}:{ident}",
        "execution_id": None,
        "classification": classification,
        "payload": {"title": title, "version": version},
        "occurred_at": "2026-11-22T00:00:00+00:00",
    }


def set_event(
    ws, cell, type_, ident, relation, targets, set_version, classification="internal"
):
    evt = lifecycle(
        ws, cell, type_, ident, verb="linked", classification=classification
    )
    evt["payload"] = {
        "relation": relation,
        "mode": "replace",
        "targets": targets,
        "set_version": set_version,
    }
    return evt


def ingest(cluster, ws: Workspace, evt: dict, publisher: dict | None = None) -> str:
    cell = evt["type"].split(".")[0]
    return cat(
        cluster,
        ws.id,
        "ingest_cell_event",
        Jsonb(evt),
        cell,
        Jsonb(publisher) if publisher else None,
    )


def live_edges(cluster, subject: str, relation: str = "in-domain") -> list[tuple]:
    return su(
        cluster,
        "SELECT to_urn, provenance FROM graph.edges WHERE from_urn = %s AND relation = %s"
        " AND retracted_at IS NULL ORDER BY to_urn",
        (subject, relation),
    )


def metric_body(cluster, ws: Workspace, key: str) -> dict[str, Any]:
    body = cat(cluster, ws.id, "catalog_get_metric", caller(ws.owner), key)
    return {
        k: v for k, v in body.items() if k not in {"urn", "updated_at", "domain_urn"}
    }


# --- concepts --------------------------------------------------------------------------------------


def test_create_domain_writes_node_version_and_audited_revision(cluster):  # AC-1, AC-10
    ws = seeded(cluster)
    assert [
        v["version"] for v in cat(cluster, ws.id, "catalog_versions", caller(ws.owner))
    ] == [1]
    made = cat(
        cluster,
        ws.id,
        "catalog_create_concept",
        caller(ws.owner),
        "domain",
        "travel",
        "การเดินทาง",
        "",
        None,
        "owner travels a lot",
    )
    assert made["urn"] == urn(ws, "domain", "travel")
    assert su(
        cluster,
        "SELECT cell, type, label FROM graph.nodes WHERE urn = %s",
        (made["urn"],),
    ) == [("platform", "domain", "การเดินทาง")]
    versions = cat(cluster, ws.id, "catalog_versions", caller(ws.owner))
    assert [(v["version"], v["note"]) for v in versions][-1] == (
        2,
        "owner travels a lot",
    )
    revisions = su(
        cluster,
        "SELECT r.audit_event_id, a.id, a.resource_id, a.after_hash IS NOT NULL FROM graph.concept_revisions r"
        " LEFT JOIN platform.audit_events a ON a.id = r.audit_event_id WHERE r.urn = %s",
        (made["urn"],),
    )
    assert len(revisions) == 1
    audit_id, joined, resource, hashed = revisions[0]
    assert audit_id is not None
    assert audit_id == joined
    assert resource == "domain/travel"
    assert hashed
    assert seed(cluster, ws.id) == 0  # re-running the seed inserts nothing


def test_rename_keeps_urn_edges_and_version(cluster):  # AC-2
    ws = seeded(cluster)
    goal = f"empire:goals:goal:{uuid.uuid4()}"
    assert (
        ingest(
            cluster,
            ws,
            set_event(
                ws,
                "goals",
                "goal",
                goal.split(":")[3],
                "in-domain",
                ["empire:platform:domain:health"],
                1,
            ),
        )
        == "edges"
    )
    cat(
        cluster,
        ws.id,
        "catalog_update_concept",
        caller(ws.owner),
        "domain",
        "health",
        {"label": "Body"},
    )
    assert [
        v["version"] for v in cat(cluster, ws.id, "catalog_versions", caller(ws.owner))
    ] == [1]
    assert [e[0] for e in live_edges(cluster, goal)] == [urn(ws, "domain", "health")]
    assert su(
        cluster,
        "SELECT count(*) FROM graph.concept_revisions WHERE urn = %s",
        (urn(ws, "domain", "health"),),
    ) == [(2,)]


def make(cluster, ws, *slugs, kind="domain"):
    for slug in slugs:
        cat(
            cluster,
            ws.id,
            "catalog_create_concept",
            caller(ws.owner),
            kind,
            slug,
            slug.title(),
            "",
            None,
            "test",
        )


def test_merge_aliases_one_hop_and_refuses_inactive_target(cluster):  # AC-3, AC-4, AC-8
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb", "cc")
    cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner),
        "domain",
        "aa",
        "bb",
        "aa is bb",
    )
    goal = str(uuid.uuid4())
    out = ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", goal, "in-domain", ["empire:platform:domain:aa"], 1
        ),
    )
    assert out == "edges"
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{goal}")] == [
        urn(ws, "domain", "bb")
    ]
    listed = {
        c["slug"]: c
        for c in cat(
            cluster, ws.id, "catalog_list_concepts", caller(ws.owner), "domain", None
        )
    }
    assert listed["bb"]["was"] == [urn(ws, "domain", "aa")]
    cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner),
        "domain",
        "bb",
        "cc",
        "bb is cc",
    )
    resolved = su(
        cluster,
        "SELECT graph.resolve(%s), graph.resolve(%s)",
        (urn(ws, "domain", "aa"), urn(ws, "domain", "bb")),
    )
    assert resolved == [(urn(ws, "domain", "cc"), urn(ws, "domain", "cc"))]
    make(cluster, ws, "dd")
    with pytest.raises(CatalogError) as err:
        cat(
            cluster,
            ws.id,
            "catalog_merge",
            caller(ws.owner),
            "domain",
            "dd",
            "aa",
            "into merged",
        )
    assert err.value.status == 409


def test_set_event_matches_edges_by_resolved_target(cluster):  # AC-5
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb")
    g1, g2 = str(uuid.uuid4()), str(uuid.uuid4())
    for g in (g1, g2):
        ingest(
            cluster,
            ws,
            set_event(
                ws, "goals", "goal", g, "in-domain", [urn(ws, "domain", "aa")], 1
            ),
        )
    cat(
        cluster, ws.id, "catalog_merge", caller(ws.owner), "domain", "aa", "bb", "merge"
    )
    ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", g1, "in-domain", ["empire:platform:domain:health"], 2
        ),
    )
    ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", g2, "in-domain", ["empire:platform:domain:bb"], 2
        ),
    )
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{g1}")] == [
        urn(ws, "domain", "health")
    ]
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{g2}")] == [
        urn(ws, "domain", "aa")
    ]
    # a non-catalog unlink of the merged target retracts the edge stored to the old URN
    task = str(uuid.uuid4())
    linked = lifecycle(ws, "tasks", "task", task, verb="linked")
    linked["payload"] = {
        "relation": "related-to",
        "target_ref": urn(ws, "domain", "health"),
        "version": 1,
    }
    ingest(cluster, ws, linked)
    make(cluster, ws, "ee")
    cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner),
        "domain",
        "ee",
        "finance",
        "merge",
    )
    unlinked = lifecycle(ws, "tasks", "task", task, verb="unlinked")
    unlinked["payload"] = {
        "relation": "related-to",
        "target_ref": urn(ws, "domain", "health"),
    }
    assert ingest(cluster, ws, unlinked) == "unlinked"
    assert live_edges(cluster, f"empire:tasks:task:{task}", "related-to") == []


def test_split_falls_back_to_unclassified_and_review_lists_requested(
    cluster,
):  # AC-6, AC-32, AC-37
    ws = seeded(cluster)
    with pytest.raises(CatalogError) as err:
        cat(
            cluster,
            ws.id,
            "catalog_split",
            caller(ws.owner),
            "domain",
            "work",
            ["skills", "finance"],
            "split",
            None,
        )
    assert (err.value.status, err.value.code) == (409, "metrics_depend")
    assert err.value.detail == {
        "metrics": ["time.work_minutes"],
        "hidden_dependencies": 0,
    }
    cat(
        cluster,
        ws.id,
        "catalog_split",
        caller(ws.owner),
        "domain",
        "work",
        ["skills", "finance"],
        "split",
        {"time.work_minutes": "finance"},
    )
    assert su(
        cluster,
        "SELECT count(*) FROM graph.aliases WHERE alias_urn = %s",
        (urn(ws, "domain", "work"),),
    ) == [(0,)]
    goal = str(uuid.uuid4())
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", goal, title="Ship the product"))
    out = ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", goal, "in-domain", ["empire:platform:domain:work"], 1
        ),
    )
    assert out == "fallback_unclassified"
    [(target, prov)] = live_edges(cluster, f"empire:goals:goal:{goal}")
    assert target == urn(ws, "domain", "unclassified")
    assert prov["requested"] == [urn(ws, "domain", "work")]
    review = cat(cluster, ws.id, "catalog_review", caller(ws.owner))["items"]
    assert any(
        i["concept"]["slug"] == "work" and i["recent_labels"] == ["Ship the product"]
        for i in review
    )
    # moving the goal to an active domain retracts the fallback edge
    ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", goal, "in-domain", ["empire:platform:domain:skills"], 2
        ),
    )
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{goal}")] == [
        urn(ws, "domain", "skills")
    ]
    # an item set to unclassified directly keeps its edge
    other = str(uuid.uuid4())
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            other,
            "in-domain",
            ["empire:platform:domain:unclassified"],
            1,
        ),
    )
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{other}")] == [
        urn(ws, "domain", "unclassified")
    ]
    # serves-value to a split value is rejected
    make(cluster, ws, "v1", "v2", "v3", kind="value")
    cat(
        cluster,
        ws.id,
        "catalog_split",
        caller(ws.owner),
        "value",
        "v1",
        ["v2", "v3"],
        "split",
        None,
    )
    out = ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", goal, "serves-value", ["empire:platform:value:v1"], 3
        ),
    )
    assert out == "rejected_relation"


def test_unknown_foreign_and_retired_targets(cluster):  # AC-7, AC-8, AC-9
    a, b = seeded(cluster), seeded(cluster)
    goal = str(uuid.uuid4())
    typo = set_event(
        a, "goals", "goal", goal, "in-domain", ["empire:platform:domain:helth"], 1
    )
    assert ingest(cluster, a, typo) == "rejected_relation"
    foreign = set_event(
        a, "goals", "goal", goal, "in-domain", [urn(b, "domain", "health")], 2
    )
    assert ingest(cluster, a, foreign) == "rejected_relation"
    short = set_event(
        a, "goals", "goal", goal, "in-domain", ["empire:platform:domain:health"], 3
    )
    assert ingest(cluster, a, short) == "edges"
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{goal}")] == [
        urn(a, "domain", "health")
    ]
    make(cluster, a, "old")
    cat(
        cluster,
        a.id,
        "catalog_retire",
        caller(a.owner),
        "domain",
        "old",
        "not used",
        None,
    )
    retired = set_event(
        a,
        "goals",
        "goal",
        str(uuid.uuid4()),
        "in-domain",
        ["empire:platform:domain:old"],
        1,
    )
    assert ingest(cluster, a, retired) == "fallback_unclassified"
    # workspace B sees only its own rows, even through the entry points
    with pytest.raises(CatalogError):
        cat(cluster, b.id, "catalog_list_concepts", caller(a.owner), None, None)
    own = cat(cluster, b.id, "catalog_list_concepts", caller(b.owner), "domain", None)
    assert {c["urn"].split(":")[3] for c in own} == {str(b.id)}
    assert (
        su(
            cluster,
            "SELECT count(*) FROM graph.concepts WHERE workspace_id = %s",
            (a.id,),
        )[0][0]
        >= 15
    )


def test_unclassified_cannot_be_merged_split_or_retired(cluster):  # AC-11
    ws = seeded(cluster)
    for fn, args in [
        ("catalog_merge", ("unclassified", "health", "x")),
        ("catalog_split", ("unclassified", ["health", "work"], "x", None)),
        ("catalog_retire", ("unclassified", "x", None)),
    ]:
        with pytest.raises(CatalogError) as err:
            cat(cluster, ws.id, fn, caller(ws.owner), "domain", *args)
        assert (err.value.status, err.value.code) == (409, "protected_concept")


def test_retire_and_merge_move_metrics(cluster):  # AC-12
    ws = seeded(cluster)
    with pytest.raises(CatalogError) as err:
        cat(
            cluster,
            ws.id,
            "catalog_retire",
            caller(ws.owner),
            "domain",
            "work",
            "drop",
            None,
        )
    assert err.value.detail["metrics"] == ["time.work_minutes"]
    before = cat(
        cluster, ws.id, "catalog_get_metric", caller(ws.owner), "time.work_minutes"
    )["version"]
    cat(
        cluster,
        ws.id,
        "catalog_retire",
        caller(ws.owner),
        "domain",
        "work",
        "drop",
        {"time.work_minutes": "skills"},
    )
    after = cat(
        cluster, ws.id, "catalog_get_metric", caller(ws.owner), "time.work_minutes"
    )
    assert (after["domain"], after["version"]) == ("skills", before + 1)
    cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner),
        "domain",
        "skills",
        "meaning",
        "merge",
    )
    moved = cat(
        cluster, ws.id, "catalog_get_metric", caller(ws.owner), "time.learning_minutes"
    )
    assert moved["domain"] == "meaning"


def test_hidden_dependencies_follow_token_scopes(cluster):  # AC-45
    ws = seeded(cluster)
    scoped = caller(
        ws.owner,
        [
            "catalog.concept:admin",
            "catalog.metric-definition:read",
            "health.health-metric:read",
        ],
    )
    with pytest.raises(CatalogError) as err:
        cat(cluster, ws.id, "catalog_retire", scoped, "domain", "health", "drop", None)
    assert err.value.detail == {
        "metrics": [
            "health.exercise_minutes",
            "health.sleep_hours",
            "health.water_litres",
            "health.weight_kg",
        ],
        "hidden_dependencies": 2,
    }
    with pytest.raises(CatalogError) as err:
        cat(
            cluster,
            ws.id,
            "catalog_retire",
            scoped,
            "domain",
            "health",
            "drop",
            {"diary.energy": "meaning"},
        )
    assert (err.value.status, err.value.code) == (422, "unknown_metric")


def test_targets_must_differ_from_the_source(cluster):  # AC-49
    ws = seeded(cluster)
    cases = [
        ("catalog_merge", ("health", "health", "x")),
        ("catalog_split", ("health", ["health", "work"], "x", None)),
        ("catalog_split", ("health", ["work"], "x", None)),
        ("catalog_retire", ("finance", "x", {"time.work_minutes": "finance"})),
    ]
    for fn, args in cases:
        with pytest.raises(CatalogError) as err:
            cat(cluster, ws.id, fn, caller(ws.owner), "domain", *args)
        assert err.value.status == 422, (fn, args)
    make(cluster, ws, "solo")
    with pytest.raises(CatalogError) as err:  # a move back to the source itself
        cat(
            cluster,
            ws.id,
            "catalog_retire",
            caller(ws.owner),
            "domain",
            "work",
            "x",
            {"time.work_minutes": "work"},
        )
    assert (err.value.status, err.value.code) == (422, "invalid_move_target")


def test_domain_and_value_may_share_a_slug(cluster):  # AC-13
    ws = seeded(cluster)
    make(cluster, ws, "health", kind="value")
    cat(
        cluster,
        ws.id,
        "catalog_update_concept",
        caller(ws.owner),
        "value",
        "health",
        {"label": "Value health"},
    )
    labels = {
        (c["kind"], c["slug"]): c["label"]
        for c in cat(
            cluster, ws.id, "catalog_list_concepts", caller(ws.owner), None, None
        )
    }
    assert labels[("value", "health")] == "Value health"
    assert labels[("domain", "health")] == "สุขภาพและพลังงาน"


def test_relation_types(cluster):  # AC-14, AC-24, AC-30
    ws = seeded(cluster)
    task = str(uuid.uuid4())
    assert (
        ingest(
            cluster,
            ws,
            set_event(
                ws,
                "tasks",
                "task",
                task,
                "serves-value",
                ["empire:platform:value:growth"],
                1,
            ),
        )
        == "rejected_relation"
    )
    kpi = str(uuid.uuid4())
    out = ingest(
        cluster,
        ws,
        set_event(
            ws,
            "operating",
            "kpi",
            kpi,
            "defined-by",
            ["empire:platform:metric:time.work_minutes"],
            1,
        ),
    )
    assert out == "edges"
    goal = f"empire:goals:goal:{uuid.uuid4()}"
    tracks = lifecycle(ws, "operating", "kpi", kpi, verb="linked")
    tracks["payload"] = {"relation": "tracks-goal", "target_ref": goal, "version": 2}
    assert ingest(cluster, ws, tracks) == "edge"
    assigned = lifecycle(ws, "tasks", "task", task, verb="linked")
    assigned["payload"] = {
        "relation": "assigned-to",
        "target_ref": f"empire:platform:identity:{ws.owner}",
        "version": 1,
    }
    assert ingest(cluster, ws, assigned) == "edge"


# --- metrics ---------------------------------------------------------------------------------------


def put(cluster, ws, key, body, if_version=None, who=None):
    return cat(
        cluster,
        ws.id,
        "catalog_put_metric",
        who or caller(ws.owner),
        key,
        Jsonb(body),
        if_version,
    )


def time_metric(**changes: Any) -> dict[str, Any]:
    body = {
        "label": "Deep work",
        "domain": "work",
        "unit": "min",
        "scale": "ratio",
        "value_range": {"min": 0, "max": 10},
        "kind": "leading",
        "objectivity": "objective",
        "source": {
            "cell": "time",
            "resource": "time-logs",
            "field": "minutes",
            "filter": {"domain": "work"},
            "time_field": "log_date",
        },
        "aggregation": "sum",
        "cadence": "daily",
        "criteria": [],
        "target": None,
    }
    body.update(changes)
    return body


def code_of(fn) -> tuple[int, str]:
    with pytest.raises(CatalogError) as err:
        fn()
    return err.value.status, err.value.code


def test_aggregation_scale_and_threshold_rules(cluster):  # AC-15, AC-16, AC-50
    ws = seeded(cluster)
    diary = metric_body(cluster, ws, "diary.energy")
    assert (
        code_of(lambda: put(cluster, ws, "diary.e2", {**diary, "aggregation": "mean"}))[
            0
        ]
        == 422
    )
    assert (
        put(cluster, ws, "diary.e2", {**diary, "aggregation": "median"})["version"] == 1
    )
    assert code_of(
        lambda: put(
            cluster,
            ws,
            "time.t1",
            time_metric(
                aggregation="last",
                source={"cell": "time", "resource": "time-logs", "field": "minutes"},
            ),
        )
    )[1] == ("time_field_required")
    nominal = time_metric(
        scale="nominal",
        aggregation="count",
        criteria=[{"level": "warn", "window": "7d", "op": "<", "value": 1}],
    )
    assert (
        code_of(lambda: put(cluster, ws, "time.t2", nominal))[1]
        == "comparison_on_nominal"
    )
    weekly = {"window": "7d", "op": ">=", "value": 14}
    assert put(cluster, ws, "time.t3", time_metric(target=weekly))["version"] == 1
    assert code_of(
        lambda: put(
            cluster, ws, "time.t4", time_metric(aggregation="mean", target=weekly)
        )
    )[1] == ("threshold_out_of_range")
    health = metric_body(cluster, ws, "health.exercise_minutes")
    capped = {
        **health,
        "value_range": {"min": 0, "max": 10},
        "aggregation": "mean",
        "target": weekly,
    }
    assert (
        put(cluster, ws, "health.e2", capped)["version"] == 1
    )  # combine = sum: per-day total unbounded
    last = json.loads(json.dumps(capped))
    last["source"]["dedupe"]["combine"] = "last"
    assert (
        code_of(lambda: put(cluster, ws, "health.e3", last))[1]
        == "threshold_out_of_range"
    )


def test_every_seeded_metric_passes_put_unchanged(cluster):  # AC-15
    ws = seeded(cluster)
    for metric in cat(
        cluster, ws.id, "catalog_list_metrics", caller(ws.owner), None, None
    ):
        body = metric_body(cluster, ws, metric["key"])
        again = put(cluster, ws, metric["key"], body, metric["version"])
        assert again["version"] == metric["version"], metric["key"]


def test_source_register_rules(cluster):  # AC-38, AC-51
    ws = seeded(cluster)
    assert (
        code_of(
            lambda: put(
                cluster,
                ws,
                "time.x",
                time_metric(
                    source={"cell": "time", "resource": "nope", "field": "minutes"}
                ),
            )
        )[1]
        == "unknown_source"
    )
    energy = metric_body(cluster, ws, "diary.energy")
    assert (
        code_of(
            lambda: put(
                cluster,
                ws,
                "diary.energy",
                {**energy, "classification": "internal"},
                energy["version"],
            )
        )[1]
        == "classification_below_source"
    )
    stored = put(
        cluster,
        ws,
        "time.y",
        time_metric(
            source={**time_metric()["source"], "read_permission": "goals.goal:read"},
            read_permission="goals.goal:read",
        ),
    )
    assert stored["read_permission"] == "time.time-log:read"
    assert "read_permission" not in stored["source"]
    assert (
        code_of(lambda: put(cluster, ws, "diary.x", time_metric()))[1]
        == "key_cell_mismatch"
    )
    assert (
        code_of(
            lambda: put(
                cluster,
                ws,
                "health.sleep2",
                {
                    **metric_body(cluster, ws, "health.sleep_hours"),
                    "source": {
                        "cell": "health",
                        "resource": "health-metrics",
                        "field": "value",
                        "filter": {"metric_type": "sleep_hours"},
                        "time_field": "recorded_at",
                    },
                },
            )
        )[1]
        == "dedupe_required"
    )
    no_tz = json.loads(json.dumps(metric_body(cluster, ws, "health.sleep_hours")))
    del no_tz["source"]["dedupe"]["tz"]
    assert (
        code_of(lambda: put(cluster, ws, "health.sleep3", no_tz))[1]
        == "dedupe_tz_required"
    )
    # a writer without the source's read permission gets the same 404 for an existing and an unknown key
    with cluster.connect("platform_api") as conn:
        writer = add_member(conn, ws, "writer", "human")
        add_role(
            conn,
            ws,
            writer,
            "writer",
            ["catalog.metric-definition:write", "catalog.metric-definition:read"],
        )
        conn.commit()
    for key in ("diary.energy", "diary.unknown"):
        assert code_of(
            lambda key=key: put(
                cluster, ws, key, energy, energy["version"], caller(writer)
            )
        ) == (404, "not_found")


def test_metric_version_and_if_version(cluster):  # AC-17, AC-44
    ws = seeded(cluster)
    m = metric_body(cluster, ws, "time.work_minutes")
    v = m["version"]
    assert put(cluster, ws, m["key"], {**m, "label": "Work time"}, v)["version"] == v
    bumped = put(cluster, ws, m["key"], {**m, "unit": "minutes"}, v)["version"]
    assert bumped == v + 1
    assert code_of(lambda: put(cluster, ws, m["key"], m, v)) == (
        409,
        "version_conflict",
    )
    cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner),
        "domain",
        "work",
        "meaning",
        "merge",
    )
    assert code_of(
        lambda: put(cluster, ws, m["key"], {**m, "unit": "minutes"}, bumped)
    ) == (409, "version_conflict")
    current = metric_body(cluster, ws, m["key"])
    relabelled = put(
        cluster, ws, m["key"], {**current, "label": "Work"}, current["version"]
    )
    assert relabelled["domain"] == "meaning"


def test_protected_definitions_are_hidden(cluster):  # AC-34, AC-46, AC-39
    ws = seeded(cluster)
    with cluster.connect("platform_api") as conn:
        reader = add_member(conn, ws, "reader", "human")
        add_role(
            conn,
            ws,
            reader,
            "reader",
            ["catalog.metric-definition:read", "catalog.concept:read"],
        )
        conn.commit()
    assert (
        code_of(
            lambda: cat(
                cluster, ws.id, "catalog_get_metric", caller(reader), "diary.energy"
            )
        )[0]
        == 404
    )
    keys = {
        m["key"]
        for m in cat(cluster, ws.id, "catalog_list_metrics", caller(reader), None, None)
    }
    assert keys == {"time.learning_minutes", "time.work_minutes"}
    assert (
        cat(cluster, ws.id, "catalog_get_metric", caller(ws.owner), "diary.energy")[
            "key"
        ]
        == "diary.energy"
    )
    scoped = caller(ws.owner, ["catalog.metric-definition:read"])
    assert (
        code_of(
            lambda: cat(cluster, ws.id, "catalog_get_metric", scoped, "diary.energy")
        )[0]
        == 404
    )
    # a time metric raised to restricted is hidden from a caller without time.time-log:read
    m = metric_body(cluster, ws, "time.learning_minutes")
    put(cluster, ws, m["key"], {**m, "classification": "restricted"}, m["version"])
    assert (
        code_of(
            lambda: cat(cluster, ws.id, "catalog_get_metric", caller(reader), m["key"])
        )[0]
        == 404
    )
    scoped_concepts = caller(ws.owner, ["catalog.metric-definition:read"])
    assert (
        code_of(
            lambda: cat(
                cluster, ws.id, "catalog_list_concepts", scoped_concepts, None, None
            )
        )[0]
        == 404
    )


def test_protected_metric_links_need_the_publishers_effective_permission(
    cluster,
):  # AC-47
    ws = seeded(cluster)
    kpi = str(uuid.uuid4())

    def link(version, publisher):
        evt = set_event(
            ws,
            "operating",
            "kpi",
            kpi,
            "defined-by",
            ["empire:platform:metric:diary.energy"],
            version,
        )
        return ingest(cluster, ws, evt, publisher)

    missing = ingest(
        cluster,
        ws,
        set_event(
            ws,
            "operating",
            "kpi",
            kpi,
            "defined-by",
            ["empire:platform:metric:diary.nothing"],
            1,
        ),
    )
    assert link(2, None) == missing == "rejected_relation"
    assert link(3, caller(ws.owner, ["graph.operating:write"])) == "rejected_relation"
    assert (
        link(4, caller(ws.owner, ["graph.operating:write", "diary.journal-entry:read"]))
        == "edges"
    )


# --- review, snapshots, privileges -----------------------------------------------------------------


def test_review_and_snapshots_follow_visibility(cluster):  # AC-18, AC-19, AC-33, AC-39
    ws = seeded(cluster)
    goal, diary = str(uuid.uuid4()), str(uuid.uuid4())
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", goal, title="Learn piano"))
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            goal,
            "in-domain",
            ["empire:platform:domain:unclassified"],
            1,
        ),
    )
    ingest(
        cluster, ws, lifecycle(ws, "diary", "entry", diary, classification="restricted")
    )
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "diary",
            "entry",
            diary,
            "in-domain",
            ["empire:platform:domain:unclassified"],
            1,
            classification="restricted",
        ),
    )
    make(cluster, ws, "bump")  # version 2 snapshots the current state
    snapshot = cat(cluster, ws.id, "catalog_versions", caller(ws.owner))[-1]
    assert snapshot["restricted_excluded"] is True
    assert snapshot["unclassified"] == [
        {"cell": "goals", "type": "goal", "classification": "internal", "count": 1}
    ]
    stored = su(
        cluster,
        "SELECT unclassified_snapshot::text FROM graph.taxonomy_versions"
        " WHERE workspace_id = %s AND version = 2",
        (ws.id,),
    )[0][0]
    assert "Learn piano" not in stored
    assert "empire:" not in stored
    review = cat(cluster, ws.id, "catalog_review", caller(ws.owner))["items"]
    assert [(i["cell"], i["count"]) for i in review] == [
        ("goals", 1)
    ]  # restricted edge without consent excluded
    with cluster.connect("platform_api") as conn:
        reader = add_member(conn, ws, "blind", "human")
        add_role(conn, ws, reader, "blind", ["catalog.concept:read"])
        conn.commit()
    assert cat(cluster, ws.id, "catalog_review", caller(reader))["items"] == []
    assert (
        cat(cluster, ws.id, "catalog_versions", caller(reader))[-1]["unclassified"]
        == []
    )
    scoped = caller(ws.owner, ["catalog.concept:read"])
    assert cat(cluster, ws.id, "catalog_review", scoped)["items"] == []


def test_privileges(cluster):  # AC-20, AC-26
    tables = [
        "concepts",
        "taxonomy_versions",
        "metric_definitions",
        "metric_sources",
        "concept_revisions",
        "metric_revisions",
        "edge_versions",
    ]
    for role in ("tasks_api", "platform_tenancy", "platform_api"):
        for table in tables:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert su(
                    cluster,
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"graph.{table}", priv),
                ) == [(False,)], (role, table, priv)
    public = su(
        cluster,
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'graph' AND (p.proname LIKE 'catalog\\_%%' OR p.proname IN"
        " ('store_metric', 'ingest_set_event')) AND has_function_privilege('public', p.oid, 'EXECUTE')",
    )
    assert public == []
    assert su(
        cluster,
        "SELECT has_function_privilege('tasks_api', 'graph.catalog_seed()', 'EXECUTE'),"
        " has_function_privilege('platform_tenancy', 'graph.catalog_seed()', 'EXECUTE'),"
        " has_function_privilege('tasks_api', 'graph.catalog_merge(jsonb, text, text, text, text)', 'EXECUTE')",
    ) == [(False, True, True)]
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        cluster.connect("empire_migrator") as conn,
    ):
        conn.execute("SELECT graph.catalog_seed()")


def test_admin_and_approval_policy(cluster):  # AC-20, AC-25
    ws = seeded(cluster)
    with cluster.connect("platform_api") as conn:
        writer = add_member(conn, ws, "editor", "human")
        add_role(
            conn,
            ws,
            writer,
            "editor",
            ["catalog.concept:write", "catalog.concept:read"],
        )
        conn.commit()
    make(cluster, ws, "aa", "bb")
    assert (
        code_of(
            lambda: cat(
                cluster,
                ws.id,
                "catalog_merge",
                caller(writer),
                "domain",
                "aa",
                "bb",
                "x",
            )
        )[0]
        == 403
    )
    with cluster.connect("platform_api") as conn:
        use_workspace(conn, ws.id)
        conn.execute(
            "INSERT INTO platform.policies (workspace_id, permission, requires_approval)"
            " VALUES (%s, 'catalog.concept:admin', true)",
            (ws.id,),
        )
        approval = new_id(conn)
        conn.execute(
            "INSERT INTO platform.approvals (id, workspace_id, requested_action, approver_identity_id,"
            " decision, decided_at) VALUES (%s, %s, 'catalog.concept:admin', %s, 'approved', now())",
            (approval, ws.id, ws.owner),
        )
        conn.commit()
    assert code_of(
        lambda: cat(
            cluster, ws.id, "catalog_merge", caller(ws.owner), "domain", "aa", "bb", "x"
        )
    ) == (403, "require_approval")
    merged = cat(
        cluster,
        ws.id,
        "catalog_merge",
        caller(ws.owner, approval=approval),
        "domain",
        "aa",
        "bb",
        "x",
    )
    assert merged["merged"]["status"] == "merged"


def test_signup_seeds_and_grants_owner_catalog(cluster):  # AC-10, AC-26, AC-28
    from test_tenancy import signup

    out = signup(cluster)
    ws, owner = uuid.UUID(out["workspace_id"]), uuid.UUID(out["identity_id"])
    versions = cat(cluster, ws, "catalog_versions", caller(owner))
    assert [v["version"] for v in versions] == [1]
    assert (
        len(cat(cluster, ws, "catalog_list_concepts", caller(owner), None, None)) == 15
    )
    assert su(
        cluster,
        "SELECT count(*) FROM graph.metric_definitions WHERE workspace_id = %s",
        (ws,),
    ) == [(8,)]
    # the signup role map holds no health or diary read permission, so only the internal time metrics show
    visible = {
        m["key"]
        for m in cat(cluster, ws, "catalog_list_metrics", caller(owner), None, None)
    }
    assert visible == {"time.learning_minutes", "time.work_minutes"}
    cat(
        cluster,
        ws,
        "catalog_create_concept",
        caller(owner),
        "domain",
        "pets",
        "Pets",
        "",
        None,
        "new",
    )
    cat(
        cluster,
        ws,
        "catalog_merge",
        caller(owner),
        "domain",
        "pets",
        "relationships",
        "fold",
    )
    assert seed(cluster, ws) == 0


# --- set event ordering -----------------------------------------------------------------------------


def test_set_events_ordered_by_set_version(cluster):  # AC-31, AC-41, AC-42
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb", "cc")
    goal = str(uuid.uuid4())
    subject = f"empire:goals:goal:{goal}"
    sets = {
        v: set_event(
            ws, "goals", "goal", goal, "in-domain", [f"empire:platform:domain:{d}"], v
        )
        for v, d in ((1, "aa"), (2, "bb"), (3, "cc"))
    }
    assert [ingest(cluster, ws, sets[v]) for v in (1, 3, 2)] == [
        "edges",
        "edges",
        "stale",
    ]
    assert [e[0] for e in live_edges(cluster, subject)] == [urn(ws, "domain", "cc")]
    single = lifecycle(ws, "goals", "goal", goal, verb="unlinked")
    single["payload"] = {"relation": "in-domain", "target_ref": urn(ws, "domain", "cc")}
    assert ingest(cluster, ws, single) == "rejected_relation"
    # a set event before .created at the same version leaves the lifecycle event in charge of the node
    fresh = str(uuid.uuid4())
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            fresh,
            "in-domain",
            ["empire:platform:domain:health"],
            1,
        ),
    )
    assert (
        ingest(cluster, ws, lifecycle(ws, "goals", "goal", fresh, title="Run 10k"))
        == "node"
    )
    assert su(
        cluster,
        "SELECT label FROM graph.nodes WHERE urn = %s",
        (f"empire:goals:goal:{fresh}",),
    ) == [("Run 10k",)]


def test_concurrent_first_sets_keep_the_highest(cluster):  # AC-42
    ws = seeded(cluster)
    goal = str(uuid.uuid4())
    events = [
        set_event(
            ws, "goals", "goal", goal, "in-domain", [f"empire:platform:domain:{d}"], v
        )
        for v, d in ((1, "work"), (2, "skills"))
    ]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run(evt):
        barrier.wait()
        results.append(ingest(cluster, ws, evt))

    threads = [threading.Thread(target=run, args=(e,)) for e in events]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) in (["edges", "edges"], ["edges", "stale"])
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{goal}")] == [
        urn(ws, "domain", "skills")
    ]


def test_deleted_subject_rejects_pending_sets(cluster):  # AC-43
    ws = seeded(cluster)
    goal = str(uuid.uuid4())
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", goal, version=1))
    ingest(
        cluster,
        ws,
        set_event(
            ws, "goals", "goal", goal, "in-domain", ["empire:platform:domain:health"], 1
        ),
    )
    assert (
        ingest(
            cluster, ws, lifecycle(ws, "goals", "goal", goal, verb="deleted", version=3)
        )
        == "deleted"
    )
    late = set_event(
        ws, "goals", "goal", goal, "in-domain", ["empire:platform:domain:work"], 2
    )
    assert ingest(cluster, ws, late) == "stale"
    assert live_edges(cluster, f"empire:goals:goal:{goal}") == []
    # a delete and a set for a subject with no node yet: no live edge after both commit, whatever the order
    ghost = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def run(evt):
        barrier.wait()
        ingest(cluster, ws, evt)

    pair = [
        lifecycle(ws, "goals", "goal", ghost, verb="deleted", version=5),
        set_event(
            ws,
            "goals",
            "goal",
            ghost,
            "in-domain",
            ["empire:platform:domain:health"],
            4,
        ),
    ]
    threads = [threading.Thread(target=run, args=(e,)) for e in pair]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert live_edges(cluster, f"empire:goals:goal:{ghost}") == []


def test_concurrent_opposite_merges(cluster):  # AC-29
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb")
    barrier = threading.Barrier(2)
    outcomes: list[int] = []

    def run(src, dst):
        barrier.wait()
        try:
            cat(
                cluster,
                ws.id,
                "catalog_merge",
                caller(ws.owner),
                "domain",
                src,
                dst,
                "race",
            )
            outcomes.append(200)
        except CatalogError as exc:
            outcomes.append(exc.status)

    threads = [
        threading.Thread(target=run, args=p) for p in (("aa", "bb"), ("bb", "aa"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [200, 409]
    ends = su(
        cluster,
        "SELECT c.status FROM graph.concepts c WHERE c.urn IN (graph.resolve(%s), graph.resolve(%s))",
        (urn(ws, "domain", "aa"), urn(ws, "domain", "bb")),
    )
    assert ends == [("active",)]


def test_retire_and_metric_put_serialise(cluster):  # AC-12 (concurrency)
    ws = seeded(cluster)
    make(cluster, ws, "side")
    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def retire():
        barrier.wait()
        try:
            cat(
                cluster,
                ws.id,
                "catalog_retire",
                caller(ws.owner),
                "domain",
                "side",
                "drop",
                None,
            )
            outcomes["retire"] = 200
        except CatalogError as exc:
            outcomes["retire"] = exc.status

    def create():
        barrier.wait()
        try:
            put(cluster, ws, "time.side", time_metric(domain="side"))
            outcomes["put"] = 200
        except CatalogError as exc:
            outcomes["put"] = exc.status

    threads = [threading.Thread(target=retire), threading.Thread(target=create)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()) in ([200, 409], [200, 422])
    assert su(
        cluster,
        "SELECT count(*) FROM graph.metric_definitions m JOIN graph.concepts c ON c.urn = m.domain_urn"
        " WHERE c.status <> 'active' AND m.workspace_id = %s",
        (ws.id,),
    ) == [(0,)]


def test_taxonomy_lock_is_shared_by_ingest(cluster):  # AC-48
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb")
    goal = str(uuid.uuid4())
    with cluster.connect("tasks_api") as merging:
        use_workspace(merging, ws.id)
        merging.execute(
            "SELECT graph.catalog_merge(%s, 'domain', 'aa', 'bb', 'hold')",
            (Jsonb(caller(ws.owner)),),
        )
        done: list[str] = []
        worker = threading.Thread(
            target=lambda: done.append(
                ingest(
                    cluster,
                    ws,
                    set_event(
                        ws,
                        "goals",
                        "goal",
                        goal,
                        "in-domain",
                        ["empire:platform:domain:aa"],
                        1,
                    ),
                )
            )
        )
        worker.start()
        worker.join(timeout=1.0)
        assert worker.is_alive()  # waits for the merge transaction
        merging.commit()
    worker.join()
    assert done == ["edges"]
    assert [e[0] for e in live_edges(cluster, f"empire:goals:goal:{goal}")] == [
        urn(ws, "domain", "bb")
    ]


# --- migration seeding of existing workspaces ------------------------------------------------------


def test_existing_workspace_is_seeded_by_the_migration(tmp_path, monkeypatch):  # AC-27
    root = tmp_path / "c"
    cluster = devdb.Cluster(root / "data", root / "sock")
    cluster.init()
    cluster.start()
    try:
        devdb.bootstrap(cluster)
        every = devdb.migration_files()
        monkeypatch.setattr(
            devdb, "migration_files", lambda: [p for p in every if p.name < "2006"]
        )
        devdb.migrate(cluster)
        with cluster.connect("platform_api") as conn:
            ws = create_workspace(conn, "before", ["tasks.task:read"])
            conn.commit()
        monkeypatch.setattr(devdb, "migration_files", lambda: every)
        assert "2006_catalog.sql" in devdb.migrate(cluster)
        with cluster.connect("empire_migrator") as conn:
            use_workspace(conn, ws.id)
            assert (
                conn.execute("SELECT count(*) FROM graph.concepts").fetchone()[0] == 15
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM graph.metric_definitions"
                ).fetchone()[0]
                == 8
            )
            perms = {
                r[0]
                for r in conn.execute(
                    "SELECT permission FROM platform.role_permissions WHERE role_id = %s",
                    (ws.owner_role,),
                )
            }
        assert set(CATALOG) <= perms
    finally:
        cluster.stop()


# --- review fixes (Codex round on 2006) ------------------------------------------------------------


def test_refused_write_keeps_its_decision_record(cluster):
    ws = seeded(cluster)
    with cluster.connect("platform_api") as conn:
        editor = add_member(conn, ws, "no-admin", "human")
        add_role(conn, ws, editor, "no-admin", ["catalog.concept:read"])
        conn.commit()
    assert code_of(
        lambda: cat(
            cluster,
            ws.id,
            "catalog_retire",
            caller(editor),
            "domain",
            "work",
            "x",
            None,
        )
    ) == (
        403,
        "deny",
    )
    assert su(
        cluster,
        "SELECT decision, reason FROM platform.authorization_decisions WHERE workspace_id = %s"
        " AND actor_identity_id = %s AND action = 'catalog.concept:admin'",
        (ws.id, editor),
    ) == [("deny", "no_permission")]


def test_kept_edge_takes_the_new_sets_classification(cluster):
    ws = seeded(cluster)
    goal = str(uuid.uuid4())
    target = ["empire:platform:domain:unclassified"]
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", goal))
    ingest(cluster, ws, set_event(ws, "goals", "goal", goal, "in-domain", target, 1))
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            goal,
            "in-domain",
            target,
            2,
            classification="restricted",
        ),
    )
    assert su(
        cluster,
        "SELECT classification FROM graph.edges WHERE from_urn = %s AND retracted_at IS NULL",
        (f"empire:goals:goal:{goal}",),
    ) == [("restricted",)]
    assert (
        cat(cluster, ws.id, "catalog_review", caller(ws.owner))["items"] == []
    )  # no consent


def test_dedupe_needed_unless_the_filter_excludes_every_shared_series(cluster):
    ws = seeded(cluster)
    sleep = metric_body(cluster, ws, "health.sleep_hours")
    plain = {k: v for k, v in sleep["source"].items() if k != "dedupe"}
    for filt in ({}, {"unit": "h"}, {"metric_type": "weight", "unit": "kg"}):
        body = {**sleep, "source": {**plain, "filter": filt}}
        assert (
            code_of(lambda body=body: put(cluster, ws, "health.s1", body))[1]
            == "dedupe_required"
        ), filt
    water = {
        **sleep,
        "source": {**plain, "filter": {"metric_type": "steps", "unit": "count"}},
    }
    assert put(cluster, ws, "health.steps", water)["version"] == 1


def test_review_counts_each_entity_once_and_keeps_direct_unclassified(cluster):
    ws = seeded(cluster)
    make(cluster, ws, "aa", "bb", "cc", "dd")
    goal = str(uuid.uuid4())
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", goal, title="Both"))
    ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            goal,
            "in-domain",
            ["empire:platform:domain:aa", "empire:platform:domain:bb"],
            1,
        ),
    )
    cat(
        cluster, ws.id, "catalog_merge", caller(ws.owner), "domain", "aa", "bb", "merge"
    )
    cat(
        cluster,
        ws.id,
        "catalog_split",
        caller(ws.owner),
        "domain",
        "bb",
        ["cc", "dd"],
        "split",
        None,
    )
    items = [
        i
        for i in cat(cluster, ws.id, "catalog_review", caller(ws.owner))["items"]
        if i["concept"]["slug"] == "bb"
    ]
    assert [(i["count"], i["recent_labels"]) for i in items] == [(1, ["Both"])]
    # a set naming a split concept (sent by its old merged URN) and unclassified directly
    other = str(uuid.uuid4())
    ingest(cluster, ws, lifecycle(ws, "goals", "goal", other, title="Direct"))
    out = ingest(
        cluster,
        ws,
        set_event(
            ws,
            "goals",
            "goal",
            other,
            "in-domain",
            ["empire:platform:domain:aa", "empire:platform:domain:unclassified"],
            1,
        ),
    )
    assert out == "fallback_unclassified"
    [(_, prov)] = live_edges(cluster, f"empire:goals:goal:{other}")
    assert prov["requested"] == [
        urn(ws, "domain", "aa")
    ]  # what the cell sent, before resolve
    slugs = {
        i["concept"]["slug"]
        for i in cat(cluster, ws.id, "catalog_review", caller(ws.owner))["items"]
        if "Direct" in i["recent_labels"]
    }
    assert slugs == {"bb", "unclassified"}


def test_signup_by_the_old_function_is_still_seeded(cluster):
    """A signups row written without the new tenancy_signup (a signup in flight during the migration)."""
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"inflight-{uuid.uuid4().hex[:8]}", ["tasks.task:read"]
        )
        conn.commit()
    with cluster.connect("empire_migrator") as conn:
        use_workspace(conn, ws.id)
        conn.execute(
            "INSERT INTO platform.signups (subject_hmac, product, workspace_id, identity_id)"
            " VALUES (%s, 'finance', %s, %s)",
            (f"h:{uuid.uuid4()}", ws.id, ws.owner),
        )
        conn.commit()
    assert [
        v["version"] for v in cat(cluster, ws.id, "catalog_versions", caller(ws.owner))
    ] == [1]


def test_http_ingest_passes_the_publisher_to_the_catalog(
    cluster, tmp_path
):  # AC-47 over HTTP
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from platform_api.routes import register
    from tasks_api.auth import TokenVerifier

    from empire_platform.devtoken import key_pair, mint

    private_pem, public_pem = key_pair(tmp_path)
    app = FastAPI()
    register(
        app,
        connect_platform=lambda: cluster.connect("tasks_api"),
        platform_verifier=TokenVerifier(
            public_pem, "catalog-test", audience="platform-api"
        ),
        graph_verifier=TokenVerifier(public_pem, "catalog-test", audience="graph-api"),
    )
    client = TestClient(app)
    ws = seeded(cluster, CATALOG + READS + ["graph.operating:write"])
    with cluster.connect("platform_api") as conn:
        bot = add_member(conn, ws, "operating-publisher", "automation")
        narrow = delegate(conn, ws, ws.owner, bot, ["graph.operating:write"])
        wide = delegate(
            conn,
            ws,
            ws.owner,
            bot,
            ["graph.operating:write", "diary.journal-entry:read"],
        )
        conn.commit()
    kpi = str(uuid.uuid4())

    def send(delegation, version):
        headers = {
            "Authorization": "Bearer "
            + mint(
                private_pem,
                sub=bot,
                ws=ws.id,
                issuer="catalog-test",
                audience="graph-api",
                delegation=delegation,
                obo=ws.owner,
            )
        }
        evt = set_event(
            ws,
            "operating",
            "kpi",
            kpi,
            "defined-by",
            ["empire:platform:metric:diary.energy"],
            version,
        )
        response = client.post("/v1/graph/events", headers=headers, json=evt)
        assert response.status_code == 200, response.text
        return response.json()["outcome"]

    assert send(narrow, 1) == "rejected_relation"
    assert send(wide, 2) == "edges"
