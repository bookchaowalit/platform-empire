"""Outbox -> Empire Graph: permission-aware, isolated, idempotent, and recoverable."""

from __future__ import annotations

import json
import threading
import time
import uuid

import psycopg
import pytest
from graph_service.consumer import GraphConsumer
from graph_world import (  # noqa: F401  (fixtures)
    apps,
    bearer,
    due_now,
    event,
    gkeys,
    gtoken,
    gworld,
    neighbors,
    run_publisher,
)


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        return conn.execute(query, params).fetchall()


def create_linked_task(apps, token, goal, title):
    tasks, _ = apps
    response = tasks.post("/v1/tasks", json={"title": title}, headers=bearer(token))
    assert response.status_code == 201, response.text
    task = response.json()
    linked = tasks.post(
        f"/v1/tasks/{task['id']}/links",
        headers=bearer(token),
        json={"relation": "supports-goal", "target_type": "goal", "target_ref": goal},
    )
    assert linked.status_code == 201
    return task


def test_task_created_through_the_api_appears_in_the_graph(
    cluster, apps, gworld, gtoken
):
    owner = gtoken(gworld.owner, gworld.ws)
    task = create_linked_task(apps, owner, gworld.goal, "Register for the race")
    run_publisher(cluster)
    _, graph = apps
    assert (task["urn"], "supports-goal", "in") in neighbors(graph, owner, gworld.goal)
    node = superuser(
        cluster,
        "SELECT label, lifecycle, version FROM graph.nodes WHERE urn = %s",
        (task["urn"],),
    )
    assert node == [("Register for the race", "active", 1)]


def test_restricted_and_confidential_neighbors_stay_hidden(
    cluster, apps, gworld, gtoken
):
    _, graph = apps
    viewer = neighbors(graph, gtoken(gworld.viewer, gworld.ws), gworld.goal)
    assert all(urn.split(":")[1] == "tasks" for urn, _, _ in viewer)
    hidden = {gworld.budget, gworld.diary}
    assert not hidden & {urn for urn, _, _ in viewer}
    search = graph.get(
        "/v1/graph/search",
        params={"q": "Race budget"},
        headers=bearer(gtoken(gworld.viewer, gworld.ws)),
    )
    assert search.json()["items"] == []


def test_restricted_edge_needs_consent_even_with_permission(
    cluster, apps, gworld, gtoken
):
    _, graph = apps
    owner = gtoken(gworld.owner, gworld.ws)
    before = {urn for urn, _, _ in neighbors(graph, owner, gworld.goal)}
    assert gworld.budget in before  # confidential, owner may read finance budgets
    assert gworld.diary not in before  # restricted edge, no consent yet
    granted = graph.post(
        "/v1/graph/consents",
        headers=bearer(owner),
        json={
            "relation": "tracks-goal",
            "subject_urn": gworld.diary,
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    assert granted.status_code == 201
    after = {urn: rel for urn, rel, _ in neighbors(graph, owner, gworld.goal)}
    assert after[gworld.diary] == "tracks-goal"
    label = [  # noqa: RUF015 - a missing node should fail as IndexError
        item
        for item in graph.get(
            "/v1/graph/neighbors", params={"urn": gworld.goal}, headers=bearer(owner)
        ).json()["items"]
        if item["urn"] == gworld.diary
    ][0]["label"]
    assert label is None  # restricted nodes never carry a label in the shared graph
    viewer = {
        urn
        for urn, _, _ in neighbors(graph, gtoken(gworld.viewer, gworld.ws), gworld.goal)
    }
    assert gworld.diary not in viewer  # consent does not grant diary permission


def test_agents_and_delegated_callers_cannot_grant_consent(apps, gworld, gtoken):
    _, graph = apps
    agent = gtoken(
        gworld.coach, gworld.ws, delegation=gworld.coach_rw, obo=gworld.owner
    )
    viewer = gtoken(gworld.viewer, gworld.ws)
    body = {
        "relation": "tracks-goal",
        "subject_urn": gworld.diary,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    assert (
        graph.post("/v1/graph/consents", headers=bearer(agent), json=body).status_code
        == 403
    )
    assert (
        graph.post("/v1/graph/consents", headers=bearer(viewer), json=body).status_code
        == 403
    )


def test_agent_sees_only_what_its_delegation_allows(cluster, apps, gworld, gtoken):
    _, graph = apps
    with_goals = gtoken(
        gworld.coach, gworld.ws, delegation=gworld.coach_rw, obo=gworld.owner
    )
    tasks_only = gtoken(
        gworld.coach, gworld.ws, delegation=gworld.coach_read, obo=gworld.owner
    )
    seen = {urn for urn, _, _ in neighbors(graph, with_goals, gworld.goal)}
    assert seen
    assert all(":tasks:" in urn for urn in seen)
    assert neighbors(graph, tasks_only, gworld.goal) == []  # cannot see the goal itself
    no_delegation = gtoken(gworld.coach, gworld.ws)
    assert neighbors(graph, no_delegation, gworld.goal) == []


def test_other_workspace_sees_nothing(apps, gworld, gtoken):
    _, graph = apps
    outsider = gtoken(gworld.outsider, gworld.beta)
    assert neighbors(graph, outsider, gworld.goal) == []
    assert (
        graph.get(
            "/v1/graph/search", params={"q": "Marathon"}, headers=bearer(outsider)
        ).json()["items"]
        == []
    )
    forged = gtoken(gworld.outsider, gworld.ws)  # signed, but not a member
    assert neighbors(graph, forged, gworld.goal) == []


def test_graph_outage_does_not_stop_tasks_and_catch_up_is_exact(
    cluster, apps, gworld, gtoken
):
    tasks, graph = apps
    owner = gtoken(gworld.owner, gworld.ws)

    def graph_down(_event):
        raise psycopg.OperationalError("graph unavailable")

    made = [
        create_linked_task(apps, owner, gworld.goal, f"Outage task {i}")
        for i in range(3)
    ]
    down = run_publisher(cluster, graph_down)
    assert down.delivered == 0
    assert down.failed >= 6
    retrying = superuser(
        cluster,
        "SELECT count(*) FROM tasks.outbox WHERE published_at IS NULL AND attempts >= 1"
        " AND subject = ANY(%s)",
        ([t["urn"] for t in made],),
    )[0][0]
    assert retrying == 6
    assert (
        tasks.get(f"/v1/tasks/{made[0]['id']}", headers=bearer(owner)).status_code
        == 200
    )  # tasks unaffected

    due_now(cluster)
    up = run_publisher(cluster)
    assert up.failed == 0
    assert up.delivered >= 6
    due_now(cluster)
    again = run_publisher(cluster)
    assert again.delivered == 0  # nothing left
    for task in made:
        assert (
            superuser(
                cluster,
                "SELECT count(*) FROM graph.nodes WHERE urn = %s",
                (task["urn"],),
            )[0][0]
            == 1
        )
        edges = superuser(
            cluster,
            "SELECT count(*) FROM graph.edges WHERE from_urn = %s AND to_urn = %s",
            (task["urn"], gworld.goal),
        )[0][0]
        assert edges == 1
    assert {t["urn"] for t in made} <= {
        urn for urn, _, _ in neighbors(graph, owner, gworld.goal)
    }


def test_redelivery_after_a_crash_is_a_no_op(cluster, apps, gworld, gtoken):
    owner = gtoken(gworld.owner, gworld.ws)
    task = create_linked_task(apps, owner, gworld.goal, "Crash task")
    consumer = GraphConsumer(lambda: cluster.connect("graph_api"))
    outcomes: list[str] = []

    def deliver_then_crash(evt):
        outcomes.append(consumer(evt))
        raise RuntimeError("publisher died before marking")

    run_publisher(cluster, deliver_then_crash)
    due_now(cluster)
    run_publisher(cluster, lambda evt: outcomes.append(consumer(evt)))
    assert outcomes.count("duplicate") >= 2
    assert (
        superuser(
            cluster, "SELECT count(*) FROM graph.nodes WHERE urn = %s", (task["urn"],)
        )[0][0]
        == 1
    )
    assert (
        superuser(
            cluster,
            "SELECT count(*) FROM graph.edges WHERE from_urn = %s",
            (task["urn"],),
        )[0][0]
        == 1
    )


def test_out_of_order_and_duplicate_events_converge(cluster, gworld):
    consumer = GraphConsumer(lambda: cluster.connect("graph_api"))
    subject = f"empire:tasks:task:{uuid.uuid4()}"
    v1 = event(
        gworld.ws,
        "tasks.task.created",
        subject,
        {"version": 1, "deleted": False, "title": "First"},
    )
    v2 = event(
        gworld.ws,
        "tasks.task.updated",
        subject,
        {"version": 2, "deleted": False, "title": "Second"},
    )
    v3 = event(
        gworld.ws, "tasks.task.deleted", subject, {"version": 3, "deleted": True}
    )
    assert consumer(v2) == "node"
    assert consumer(v1) == "stale"
    assert consumer(v2) == "duplicate"
    assert superuser(
        cluster, "SELECT label, version FROM graph.nodes WHERE urn = %s", (subject,)
    ) == [("Second", 2)]
    assert consumer(v3) == "deleted"
    late = event(
        gworld.ws,
        "tasks.task.updated",
        subject,
        {"version": 2, "deleted": False, "title": "Late"},
    )
    assert consumer(late) == "stale"
    assert superuser(
        cluster, "SELECT lifecycle, label FROM graph.nodes WHERE urn = %s", (subject,)
    ) == [("deleted", None)]
    ghost = f"empire:tasks:task:{uuid.uuid4()}"
    assert (
        consumer(
            event(
                gworld.ws, "tasks.task.deleted", ghost, {"version": 2, "deleted": True}
            )
        )
        == "deleted"
    )
    assert (
        consumer(
            event(
                gworld.ws,
                "tasks.task.created",
                ghost,
                {"version": 1, "deleted": False, "title": "Ghost"},
            )
        )
        == "stale"
    )


def ingest_racing_a_newer_event(cluster, ws, newer, older):
    """Leave ``newer`` uncommitted, ingest ``older`` from a second connection
    until it blocks on the node's key, then commit ``newer``."""
    result = {}

    def ingest(conn, evt):
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws),))
        return conn.execute(
            "SELECT graph.ingest_event(%s::jsonb)", (json.dumps(evt),)
        ).fetchone()[0]

    def second():
        with cluster.connect("graph_api") as conn:
            result["older"] = ingest(conn, older)

    with cluster.connect("graph_api") as first:
        result["newer"] = ingest(first, newer)
        worker = threading.Thread(target=second)
        worker.start()
        deadline = time.monotonic() + 10
        while not superuser(
            cluster,
            "SELECT 1 FROM pg_stat_activity WHERE usename = 'graph_api'"
            " AND wait_event_type = 'Lock'",
        ):
            assert time.monotonic() < deadline, (
                "the older event never waited for the newer one"
            )
            time.sleep(0.02)
        first.commit()
    worker.join(10)
    return result


def test_older_event_for_a_new_subject_never_overwrites_a_concurrent_newer_one(
    cluster, gworld
):
    subject = f"empire:tasks:task:{uuid.uuid4()}"
    v1 = event(
        gworld.ws,
        "tasks.task.created",
        subject,
        {"version": 1, "deleted": False, "title": "First"},
    )
    v2 = event(
        gworld.ws,
        "tasks.task.updated",
        subject,
        {"version": 2, "deleted": False, "title": "Second"},
    )
    assert ingest_racing_a_newer_event(cluster, gworld.ws, v2, v1) == {
        "newer": "node",
        "older": "stale",
    }
    assert superuser(
        cluster,
        "SELECT label, lifecycle, version FROM graph.nodes WHERE urn = %s",
        (subject,),
    ) == [("Second", "active", 2)]


def test_older_deletion_for_a_new_subject_neither_deletes_nor_retracts(cluster, gworld):
    subject = f"empire:tasks:task:{uuid.uuid4()}"
    linked = event(
        gworld.ws,
        "tasks.task.linked",
        subject,
        {
            "version": 2,
            "deleted": False,
            "title": "Kept",
            "relation": "supports-goal",
            "target_type": "goal",
            "target_ref": gworld.goal,
        },
    )
    deleted = event(
        gworld.ws, "tasks.task.deleted", subject, {"version": 1, "deleted": True}
    )
    assert ingest_racing_a_newer_event(cluster, gworld.ws, linked, deleted) == {
        "newer": "edge",
        "older": "stale",
    }
    assert superuser(
        cluster,
        "SELECT label, lifecycle, version FROM graph.nodes WHERE urn = %s",
        (subject,),
    ) == [("Kept", "active", 2)]
    assert superuser(
        cluster, "SELECT retracted_at FROM graph.edges WHERE from_urn = %s", (subject,)
    ) == [(None,)]


def test_older_update_never_revives_a_concurrent_newer_deletion(cluster, gworld):
    subject = f"empire:tasks:task:{uuid.uuid4()}"
    deleted = event(
        gworld.ws, "tasks.task.deleted", subject, {"version": 2, "deleted": True}
    )
    v1 = event(
        gworld.ws,
        "tasks.task.created",
        subject,
        {"version": 1, "deleted": False, "title": "Ghost"},
    )
    assert ingest_racing_a_newer_event(cluster, gworld.ws, deleted, v1) == {
        "newer": "deleted",
        "older": "stale",
    }
    assert superuser(
        cluster,
        "SELECT label, lifecycle, version FROM graph.nodes WHERE urn = %s",
        (subject,),
    ) == [(None, "deleted", 2)]


def test_unregistered_relation_endpoints_are_rejected(cluster, gworld):
    consumer = GraphConsumer(lambda: cluster.connect("graph_api"))
    subject = f"empire:tasks:task:{uuid.uuid4()}"
    bad = event(
        gworld.ws,
        "tasks.task.linked",
        subject,
        {
            "version": 1,
            "deleted": False,
            "title": "x",
            "relation": "supports-goal",
            "target_type": "project",
            "target_ref": "empire:projects:project:p1",
        },
    )
    assert consumer(bad) == "rejected_relation"
    assert (
        superuser(
            cluster, "SELECT count(*) FROM graph.edges WHERE from_urn = %s", (subject,)
        )[0][0]
        == 0
    )


def test_ingest_refuses_an_event_for_another_workspace(cluster, gworld):
    with cluster.connect("graph_api") as conn:
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(gworld.beta),)
        )
        bad = event(
            gworld.ws,
            "tasks.task.created",
            f"empire:tasks:task:{uuid.uuid4()}",
            {"version": 1, "title": "x"},
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT graph.ingest_event(%s::jsonb)", (json.dumps(bad),))


def test_graph_and_publisher_roles_are_least_privilege(cluster):
    with cluster.connect("graph_api") as conn:
        for statement in (
            "SELECT count(*) FROM tasks.tasks",
            "SELECT count(*) FROM platform.memberships",
            "ALTER TABLE graph.nodes DISABLE ROW LEVEL SECURITY",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
    with cluster.connect("tasks_publisher") as conn:
        for statement in (
            "SELECT count(*) FROM tasks.tasks",
            "SELECT count(*) FROM graph.nodes",
            "UPDATE tasks.outbox SET payload = '{}'::jsonb",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
