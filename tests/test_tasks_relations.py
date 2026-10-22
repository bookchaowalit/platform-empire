"""Read filters, stats, links, assignments, role suggestions, and task types.

These are what the SQLite-era readers used; each must respect the caller's
workspace and the narrow write permissions.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from conftest import add_member, create_workspace, delegate
from fastapi.testclient import TestClient
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER

ISSUER = "empire-relations-test"
GOAL = "empire:goals:goal:legacy-7"


@pytest.fixture(scope="module")
def setup(cluster, tmp_path_factory):
    private_pem, public_pem = key_pair(tmp_path_factory.mktemp("rel-keys"))
    client = TestClient(
        create_app(
            lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)
        )
    )
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        ws = create_workspace(conn, f"rel-{suffix}", list(TASKS_OWNER))
        bot = add_member(conn, ws, "router", "automation")
        reader = delegate(conn, ws, ws.owner, bot, ["tasks.task:read"])
        router = delegate(
            conn,
            ws,
            ws.owner,
            bot,
            [
                "tasks.task:read",
                "tasks.assignment:write",
                "tasks.suggestion:write",
                "tasks.link:write",
            ],
        )
        other = create_workspace(conn, f"rel-other-{suffix}", list(TASKS_OWNER))
        conn.commit()

    def headers(sub=None, workspace=None, delegation=None):
        return {
            "Authorization": "Bearer "
            + mint(
                private_pem,
                sub=sub or ws.owner,
                ws=workspace or ws.id,
                delegation=delegation,
                obo=ws.owner if delegation else None,
                issuer=ISSUER,
            )
        }

    owner = headers()
    today = date.today()
    made = {}
    for key, body in {
        "a": {
            "title": "Write 100% report",
            "project": "alpha",
            "priority": 1,
            "due_date": str(today - timedelta(days=2)),
        },
        "b": {
            "title": "Review",
            "description": "book dev loop",
            "project": "alpha",
            "priority": 3,
            "due_date": str(today + timedelta(days=3)),
        },
        "c": {"title": "Other", "project": "beta", "category": "ops", "priority": 2},
    }.items():
        made[key] = client.post("/v1/tasks", json=body, headers=owner).json()
    child = client.post(
        "/v1/tasks",
        json={"title": "Child", "parent_task_id": made["a"]["id"]},
        headers=owner,
    ).json()
    made["child"] = child
    client.post(f"/v1/tasks/{made['c']['id']}/complete", headers=owner)
    return (
        client,
        headers,
        owner,
        made,
        {"bot": bot, "reader": reader, "router": router, "other": other},
    )


def numbers(response):
    assert response.status_code == 200, response.text
    return sorted(t["number"] for t in response.json()["items"])


def test_filters_match_the_legacy_queries(setup):
    client, _, owner, made, _ = setup
    a, b, c, child = (made[k]["number"] for k in ("a", "b", "c", "child"))
    today = date.today()

    def get(**params):
        return numbers(client.get("/v1/tasks", params=params, headers=owner))

    assert get(project="alpha") == [a, b]
    assert get(priority_max=2) == [a, c]
    assert get(due_from=str(today), due_before=str(today + timedelta(days=7))) == [b]
    assert get(numbers=[a, c, 999999]) == [a, c]
    assert get(parent=a) == [child]
    assert get(q="book DEV") == [b]
    assert get(q="100%") == [a]  # wildcards in the query are literal
    assert get(q="_") == []
    assert get(status=["done"], category="ops") == [c]
    assert get(no_external_source=True, numbers=[a]) == [a]
    assert (
        client.get(f"/v1/tasks/{made['child']['id']}", headers=owner).json()[
            "parent_number"
        ]
        == a
    )


def test_stats_count_and_group(setup):
    client, _, owner, _made, _ = setup
    stats = client.get(
        "/v1/tasks/stats",
        params={"group_by": "project", "project": "alpha"},
        headers=owner,
    ).json()
    assert stats["total"] == 2
    assert stats["groups"] == [{"key": "alpha", "count": 2}]
    by_status = client.get(
        "/v1/tasks/stats", params={"group_by": "status"}, headers=owner
    ).json()
    assert {g["key"]: g["count"] for g in by_status["groups"]} == {"todo": 3, "done": 1}
    assert (
        client.get(
            "/v1/tasks/stats", params={"group_by": "title"}, headers=owner
        ).status_code
        == 422
    )


def test_links_list_by_task_and_target_and_keep_attributes(setup):
    client, _, owner, made, _ = setup
    task = made["a"]
    link = {"relation": "supports-goal", "target_type": "goal", "target_ref": GOAL}
    client.post(
        f"/v1/tasks/{task['id']}/links",
        json={**link, "attributes": {"link_type": "primary"}},
        headers=owner,
    )
    client.post(
        f"/v1/tasks/{task['id']}/links", json=link, headers=owner
    )  # no attributes: keep stored ones
    items = client.get(
        "/v1/task-links", params={"target_ref": GOAL}, headers=owner
    ).json()["items"]
    assert [(i["task_number"], i["attributes"]) for i in items] == [
        (task["number"], {"link_type": "primary"})
    ]
    assert (
        client.get(
            "/v1/task-links", params={"task": made["b"]["number"]}, headers=owner
        ).json()["items"]
        == []
    )
    client.post(
        f"/v1/tasks/{task['id']}/links",
        json={**link, "attributes": {"link_type": "support"}},
        headers=owner,
    )
    items = client.get(
        "/v1/task-links", params={"task": task["number"]}, headers=owner
    ).json()["items"]
    assert items[0]["attributes"] == {"link_type": "support"}


def test_assignments_are_idempotent_and_need_their_permission(setup):
    client, headers, _owner, made, ids = setup
    task = made["b"]
    body = {
        "assignee_type": "role",
        "assignee_ref": "role.engineer",
        "assignee_name": "Engineer",
    }
    router = headers(sub=ids["bot"], delegation=ids["router"])
    reader = headers(sub=ids["bot"], delegation=ids["reader"])
    assert (
        client.put(
            f"/v1/tasks/{task['id']}/assignments", json=body, headers=reader
        ).status_code
        == 403
    )
    first = client.put(
        f"/v1/tasks/{task['id']}/assignments", json=body, headers=router
    ).json()
    second = client.put(
        f"/v1/tasks/{task['id']}/assignments", json=body, headers=router
    ).json()
    assert (first["outcome"], second["outcome"]) == ("created", "unchanged")
    items = client.get(
        "/v1/task-assignments", params={"assignee_type": "role"}, headers=reader
    ).json()["items"]
    assert [(i["task_number"], i["assignee_ref"]) for i in items] == [
        (task["number"], "role.engineer")
    ]
    # The router cannot change the task itself.
    assert (
        client.patch(
            f"/v1/tasks/{task['id']}", json={"title": "x"}, headers=router
        ).status_code
        == 403
    )


def test_role_suggestions_upsert_review_and_compare_and_set(setup):
    client, headers, owner, made, ids = setup
    task = made["a"]
    router = headers(sub=ids["bot"], delegation=ids["router"])
    suggestion = {
        "role_id": "role.writer",
        "source_registry": "docs/ROLE-TASK-ROUTING.yaml",
        "confidence": "high",
        "match_reason": "title mentions report",
    }

    def put(body):
        return client.put(
            f"/v1/tasks/{task['id']}/role-suggestions", json=body, headers=router
        ).json()

    assert put(suggestion)["outcome"] == "created"
    assert put(suggestion)["outcome"] == "unchanged"
    review = {
        "role_id": "role.writer",
        "source_registry": suggestion["source_registry"],
        "review_status": "accepted",
        "expected_status": "suggested",
    }

    def patch(body):
        return client.patch(
            f"/v1/tasks/{task['id']}/role-suggestions", json=body, headers=router
        ).json()

    assert patch(review)["outcome"] == "updated"
    assert patch(review)["outcome"] == "unchanged"  # no longer 'suggested'
    # A later resync with new evidence keeps the accepted decision.
    assert (
        put({**suggestion, "match_reason": "also in project"})["outcome"] == "updated"
    )
    items = client.get(
        "/v1/task-role-suggestions", params={"review_status": "accepted"}, headers=owner
    ).json()["items"]
    assert [(i["task_number"], i["role_id"], i["match_reason"]) for i in items] == [
        (task["number"], "role.writer", "also in project")
    ]


def test_task_types_and_other_workspaces_see_nothing(setup, cluster):
    client, headers, owner, _made, ids = setup
    assert client.get("/v1/task-types", headers=owner).status_code == 200
    outsider = headers(sub=ids["other"].owner, workspace=ids["other"].id)
    for path, params in [
        ("/v1/tasks", {}),
        ("/v1/task-links", {}),
        ("/v1/task-assignments", {}),
        ("/v1/task-role-suggestions", {}),
        ("/v1/task-types", {}),
    ]:
        assert (
            client.get(path, params=params, headers=outsider).json()["items"] == []
        ), path
    assert client.get("/v1/tasks/stats", headers=outsider).json()["total"] == 0


def test_relation_writes_publish_events(setup, cluster):
    _, _, _, made, _ = setup
    with cluster.connect("postgres") as conn:
        types = {
            row[0]
            for row in conn.execute(
                "SELECT type FROM tasks.outbox WHERE subject = ANY(%s)",
                ([f"empire:tasks:task:{made[k]['id']}" for k in ("a", "b")],),
            )
        }
    assert {
        "tasks.task.linked",
        "tasks.task.assigned",
        "tasks.task.role-suggested",
    } <= types


def test_withdrawals_remove_only_what_is_asked(setup):
    client, headers, owner, made, ids = setup
    task = made["b"]
    router = headers(sub=ids["bot"], delegation=ids["router"])
    link = {
        "relation": "related-to",
        "target_type": "skill",
        "target_ref": "empire:roles-skills:skill:legacy-11",
    }
    client.post(f"/v1/tasks/{task['id']}/links", json=link, headers=router)
    params = {"relation": link["relation"], "target_ref": link["target_ref"]}
    assert (
        client.delete(
            f"/v1/tasks/{task['id']}/links", params=params, headers=router
        ).json()["outcome"]
        == "removed"
    )
    assert (
        client.delete(
            f"/v1/tasks/{task['id']}/links", params=params, headers=router
        ).json()["outcome"]
        == "absent"
    )
    registry = "docs/ROLE-TASK-ROUTING.yaml"
    for role in ("role.a", "role.b"):
        client.put(
            f"/v1/tasks/{task['id']}/role-suggestions",
            headers=router,
            json={
                "role_id": role,
                "source_registry": registry,
                "confidence": "low",
                "match_reason": "x",
            },
        )
    client.patch(
        f"/v1/tasks/{task['id']}/role-suggestions",
        headers=router,
        json={
            "role_id": "role.b",
            "source_registry": registry,
            "review_status": "accepted",
        },
    )

    def withdraw(role):
        return client.delete(
            f"/v1/tasks/{task['id']}/role-suggestions",
            headers=router,
            params={
                "role_id": role,
                "source_registry": registry,
                "only_status": "suggested",
            },
        ).json()["outcome"]

    assert (withdraw("role.a"), withdraw("role.b")) == (
        "removed",
        "absent",
    )  # the accepted one stays
    left = client.get(
        "/v1/task-role-suggestions",
        params={"task": task["number"], "source_registry": registry},
        headers=owner,
    ).json()["items"]
    assert [i["role_id"] for i in left] == ["role.b"]
    reader = headers(sub=ids["bot"], delegation=ids["reader"])
    assert (
        client.delete(
            f"/v1/tasks/{task['id']}/links", params=params, headers=reader
        ).status_code
        == 403
    )


def test_unlink_retracts_the_graph_edge(setup, cluster):
    from graph_world import run_publisher

    client, _, owner, made, _ = setup
    task = made["c"]
    link = {
        "relation": "supports-goal",
        "target_type": "goal",
        "target_ref": "empire:goals:goal:legacy-9",
    }
    client.post(f"/v1/tasks/{task['id']}/links", json=link, headers=owner)
    run_publisher(cluster)
    client.delete(
        f"/v1/tasks/{task['id']}/links",
        params={"relation": link["relation"], "target_ref": link["target_ref"]},
        headers=owner,
    )
    run_publisher(cluster)
    with cluster.connect("postgres") as conn:
        retracted = conn.execute(
            "SELECT retracted_at IS NOT NULL FROM graph.edges WHERE from_urn = %s AND to_urn = %s",
            (task["urn"], link["target_ref"]),
        ).fetchall()
    assert retracted == [(True,)]


def test_reopening_clears_completion(setup):
    client, _, owner, _, _ = setup
    task = client.post("/v1/tasks", json={"title": "Reopen me"}, headers=owner).json()
    client.post(f"/v1/tasks/{task['id']}/complete", headers=owner)
    reopened = client.patch(
        f"/v1/tasks/{task['id']}", json={"status": "todo"}, headers=owner
    ).json()
    assert reopened["status"] == "todo"
    assert reopened["completed_at"] is None
    key = f"/v1/tasks/external/probe/{uuid.uuid4().hex[:8]}"
    assert client.put(
        key, json={"title": "Upstream", "status": "done"}, headers=owner
    ).json()["task"]["completed_at"]
    again = client.put(
        key, json={"title": "Upstream", "status": "in_progress"}, headers=owner
    ).json()["task"]
    assert again["status"] == "in_progress"
    assert again["completed_at"] is None


def test_external_identity_is_set_once(setup):
    client, _, owner, _, _ = setup
    first = client.post("/v1/tasks", json={"title": "Pushed out"}, headers=owner).json()
    other = client.post("/v1/tasks", json={"title": "Another"}, headers=owner).json()
    ref = f"td-{uuid.uuid4().hex[:8]}"

    def put(task, ext):
        return client.put(
            f"/v1/tasks/{task['id']}/external-identity",
            json={"source": "todoist", "external_id": ext},
            headers=owner,
        )

    bound = put(first, ref)
    assert bound.status_code == 200
    assert (
        bound.json()["external_source"],
        bound.json()["external_id"],
    ) == ("todoist", ref)
    assert put(first, ref).status_code == 200  # same identity: no-op
    assert put(first, "different").status_code == 409  # already bound
    assert put(other, ref).status_code == 409  # identity used by another task
    found = client.get(f"/v1/tasks/external/todoist/{ref}", headers=owner).json()
    assert found["id"] == first["id"]


def test_upsert_with_if_match_never_clobbers_a_newer_edit(setup):
    client, _, owner, _, _ = setup
    key = f"/v1/tasks/external/probe/{uuid.uuid4().hex[:8]}"
    task = client.put(key, json={"title": "Remote v1"}, headers=owner).json()["task"]
    seen = task["version"]
    client.patch(
        f"/v1/tasks/{task['id']}", json={"title": "Edited locally"}, headers=owner
    )
    stale = client.put(
        key, json={"title": "Remote v2"}, headers={**owner, "If-Match": str(seen)}
    )
    assert stale.status_code == 412
    current = client.get(f"/v1/tasks/{task['id']}", headers=owner).json()
    assert current["title"] == "Edited locally"
    fresh = client.put(
        key,
        json={"title": "Remote v2"},
        headers={**owner, "If-Match": str(current["version"])},
    )
    assert fresh.status_code == 200
    assert fresh.json()["task"]["title"] == "Remote v2"
    missing = f"/v1/tasks/external/probe/{uuid.uuid4().hex[:8]}"
    assert (
        client.put(
            missing, json={"title": "x"}, headers={**owner, "If-Match": "1"}
        ).status_code
        == 412
    )
    assert (
        client.get(
            missing.replace("/v1/tasks/external", "/v1/tasks/external"), headers=owner
        ).status_code
        == 404
    )


def test_dependencies_are_idempotent_and_validated(setup):
    client, headers, owner, made, ids = setup
    a, b = made["a"], made["b"]

    def put(task, body):
        return client.put(
            f"/v1/tasks/{task['id']}/dependencies", json=body, headers=owner
        )

    assert put(b, {"depends_on": a["number"]}).json()["outcome"] == "created"
    assert put(b, {"depends_on": a["number"]}).json()["outcome"] == "unchanged"
    assert put(b, {"depends_on": b["number"]}).status_code == 422  # no self dependency
    assert put(b, {"depends_on": 999999}).status_code == 422
    items = client.get(
        "/v1/task-dependencies", params={"task": b["number"]}, headers=owner
    ).json()["items"]
    assert items == [
        {
            "task_number": b["number"],
            "depends_on_number": a["number"],
            "dependency_type": "finish_to_start",
        }
    ]
    reader = headers(sub=ids["bot"], delegation=ids["reader"])
    assert put.__call__
    assert (
        client.put(
            f"/v1/tasks/{a['id']}/dependencies",
            json={"depends_on": b["number"]},
            headers=reader,
        ).status_code
        == 403
    )
