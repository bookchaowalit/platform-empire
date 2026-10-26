"""Automation identities: least privilege per writer, revocation and expiry mid-run."""

from __future__ import annotations

import copy
import uuid

import pytest
from conftest import create_workspace
from fastapi.testclient import TestClient
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

from empire_platform.automations import load_registry, provision
from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import CELL_OWNERS

ISSUER = "empire-automation-test"
REGISTRY = load_registry()
OPERATIONS = {
    # operation -> permission it needs
    "create": "tasks.task:write",
    "upsert": "tasks.task:write",
    "edit_notes": "tasks.task:write",
    "move_due_date": "tasks.status:write",
    "complete": "tasks.status:write",
    "set_calendar_event": "tasks.calendar:write",
    "link": "tasks.link:write",
    "archive": "tasks.archive:write",
    "reminder_delivered": "tasks.reminder:write",
    "generate_occurrences": "tasks.occurrence:write",
    "assign": "tasks.assignment:write",
    "suggest_role": "tasks.suggestion:write",
    "delete": "tasks.task:delete",
}


@pytest.fixture(scope="module")
def env(cluster, tmp_path_factory):
    private_pem, public_pem = key_pair(tmp_path_factory.mktemp("automation-keys"))
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn,
            f"automations-{uuid.uuid4().hex[:8]}",
            [p for perms in CELL_OWNERS.values() for p in perms],
        )
        conn.commit()
    client = TestClient(
        create_app(
            lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)
        )
    )

    def token(sub, delegation=None, obo=None):
        return {
            "Authorization": "Bearer "
            + mint(
                private_pem,
                sub=sub,
                ws=ws.id,
                delegation=delegation,
                obo=obo,
                issuer=ISSUER,
            )
        }

    return ws, client, token


def owner_task(env, **fields):
    ws, client, token = env
    response = client.post(
        "/v1/tasks",
        json={
            "title": "Fixture",
            "recurrence_rule": "weekly",
            "due_date": "2026-10-01",
            "reminder_at": "2026-10-10T09:00:00+00:00",
            **fields,
        },
        headers=token(ws.owner),
    )
    assert response.status_code == 201, response.text
    return response.json()


def attempt(client, headers, operation, task) -> int:
    path = f"/v1/tasks/{task['id']}"
    calls = {
        "create": lambda: client.post(
            "/v1/tasks", json={"title": "A"}, headers=headers
        ),
        "upsert": lambda: client.put(
            f"/v1/tasks/external/probe/{uuid.uuid4().hex}",
            json={"title": "B"},
            headers=headers,
        ),
        "edit_notes": lambda: client.patch(path, json={"notes": "n"}, headers=headers),
        "move_due_date": lambda: client.patch(
            path, json={"due_date": "2026-12-01"}, headers=headers
        ),
        "complete": lambda: client.post(f"{path}/complete", headers=headers),
        "set_calendar_event": lambda: client.patch(
            path, json={"calendar_event_id": "cal-1"}, headers=headers
        ),
        "link": lambda: client.post(
            f"{path}/links",
            headers=headers,
            json={
                "relation": "related-to",
                "target_type": "goal",
                "target_ref": "empire:goals:goal:g",
            },
        ),
        "archive": lambda: client.post(
            f"{path}/archive", json={"reason": "old"}, headers=headers
        ),
        "reminder_delivered": lambda: client.post(
            f"{path}/reminders/delivered",
            headers=headers,
            json={"reminder_at": "2026-10-10T09:00:00+00:00", "channel": "line"},
        ),
        "generate_occurrences": lambda: client.post(
            f"{path}/occurrences/generate",
            json={"until": "2026-10-15"},
            headers=headers,
        ),
        "assign": lambda: client.put(
            f"{path}/assignments",
            headers=headers,
            json={
                "assignee_type": "role",
                "assignee_ref": f"role.{uuid.uuid4().hex[:6]}",
            },
        ),
        "suggest_role": lambda: client.put(
            f"{path}/role-suggestions",
            headers=headers,
            json={
                "role_id": f"role.{uuid.uuid4().hex[:6]}",
                "source_registry": "probe",
                "confidence": "low",
                "match_reason": "probe",
            },
        ),
        "delete": lambda: client.delete(path, headers=headers),
    }
    return calls[operation]().status_code


@pytest.mark.parametrize(
    "automation",
    REGISTRY["automations"],
    ids=[a["id"] for a in REGISTRY["automations"]],
)
def test_each_automation_can_do_exactly_its_scopes(cluster, env, automation):
    ws, client, token = env
    items = {item.automation: item for item in provision(cluster, ws.id)}
    item = items[automation["id"]]
    headers = token(item.identity, item.delegation, item.owner)
    for operation, permission in OPERATIONS.items():
        task = owner_task(env)
        status = attempt(client, headers, operation, task)
        allowed = permission in automation["scopes"] and operation != "delete"
        if allowed:
            assert status in (200, 201), (automation["id"], operation, status)
        else:
            # Without tasks.task:read the task does not exist for the caller (decision cells' automations).
            refused = (
                (403, 409)
                if "tasks.task:read" in automation["scopes"]
                else (403, 404, 409)
            )
            assert status in refused, (automation["id"], operation, status)


def test_no_automation_can_delete(env):
    assert all("tasks.task:delete" not in a["scopes"] for a in REGISTRY["automations"])
    reconciler = next(
        a for a in REGISTRY["automations"] if a["id"] == "overdue-reconciler"
    )
    assert "tasks.archive:write" in reconciler["scopes"]


def test_revocation_mid_run_stops_the_next_call(cluster, env):
    ws, client, token = env
    item = next(
        i for i in provision(cluster, ws.id) if i.automation == "engagement-sync"
    )
    headers = token(item.identity, item.delegation, item.owner)
    first = client.put(
        "/v1/tasks/external/engagements/E-1",
        json={"title": "Next action"},
        headers=headers,
    )
    assert first.status_code in (200, 201)
    with cluster.connect("platform_api") as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws.id),))
        conn.execute(
            "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
            (item.delegation,),
        )
        conn.commit()
    second = client.put(
        "/v1/tasks/external/engagements/E-2", json={"title": "Other"}, headers=headers
    )
    assert second.status_code == 403
    assert second.json()["detail"]["reason"] == "delegation_invalid"
    retry = client.put(
        "/v1/tasks/external/engagements/E-1",
        json={"title": "Next action"},
        headers=headers,
    )
    assert retry.status_code == 403
    # Re-provisioning issues a fresh delegation; the first record was not duplicated.
    renewed = next(
        i for i in provision(cluster, ws.id) if i.automation == "engagement-sync"
    )
    assert renewed.renewed
    assert renewed.delegation != item.delegation
    again = client.put(
        "/v1/tasks/external/engagements/E-1",
        json={"title": "Next action"},
        headers=token(renewed.identity, renewed.delegation, renewed.owner),
    )
    assert again.json()["outcome"] == "unchanged"


def test_expired_delegation_is_refused(cluster, env):
    ws, client, token = env
    item = next(i for i in provision(cluster, ws.id) if i.automation == "calendar-sync")
    with cluster.connect("postgres") as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws.id),))
        conn.execute(
            "UPDATE platform.delegations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (item.delegation,),
        )
        conn.commit()
    task = owner_task(env)
    response = client.patch(
        f"/v1/tasks/{task['id']}",
        json={"calendar_event_id": "x"},
        headers=token(item.identity, item.delegation, item.owner),
    )
    # Without a valid delegation the automation cannot read the task, so it learns nothing about it.
    assert response.status_code == 404
    with cluster.connect("postgres") as conn:
        reason = conn.execute(
            "SELECT reason FROM platform.authorization_decisions WHERE delegation_id = %s"
            " ORDER BY decided_at DESC LIMIT 1",
            (item.delegation,),
        ).fetchone()[0]
    assert reason == "delegation_invalid"
    renewed = next(
        i for i in provision(cluster, ws.id) if i.automation == "calendar-sync"
    )
    assert renewed.renewed


def test_provisioning_is_idempotent_and_follows_scope_changes(cluster, env):
    ws, _, _ = env
    first = {i.automation: i for i in provision(cluster, ws.id)}
    second = {i.automation: i for i in provision(cluster, ws.id)}
    assert all(
        not i.renewed and i.delegation == first[name].delegation
        for name, i in second.items()
    )
    narrowed = copy.deepcopy(REGISTRY)
    next(a for a in narrowed["automations"] if a["id"] == "sales-pipeline")[
        "scopes"
    ] = ["tasks.task:read"]
    third = {i.automation: i for i in provision(cluster, ws.id, narrowed)}
    assert third["sales-pipeline"].renewed
    assert third["sales-pipeline"].scopes == ("tasks.task:read",)
    assert not third["decision-review"].renewed
    with cluster.connect("postgres") as conn:
        active = conn.execute(
            "SELECT count(*) FROM platform.delegations WHERE grantee_identity_id = %s AND revoked_at IS NULL",
            (third["sales-pipeline"].identity,),
        ).fetchone()[0]
    assert active == 1


def test_provision_only_touches_the_named_automations(cluster):
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn,
            f"only-{uuid.uuid4().hex[:8]}",
            [p for perms in CELL_OWNERS.values() for p in perms],
        )
        conn.commit()
    items = provision(cluster, ws.id, only={"deploy-verifier"})
    assert [i.automation for i in items] == ["deploy-verifier"]
    with cluster.connect("postgres") as conn:
        others = conn.execute(
            "SELECT count(*) FROM platform.identities i JOIN platform.memberships m ON m.identity_id = i.id"
            " WHERE m.workspace_id = %s AND i.kind = 'automation' AND i.display_name <> 'deploy-verifier'",
            (ws.id,),
        ).fetchone()[0]
    assert others == 0
    with pytest.raises(KeyError):
        provision(cluster, ws.id, only={"no-such-automation"})


def test_deploy_verifier_only_reads():
    verifier = next(a for a in REGISTRY["automations"] if a["id"] == "deploy-verifier")
    assert verifier["scopes"]
    assert all(scope.endswith(":read") for scope in verifier["scopes"])
    # sensitive personal data stays out of reach: the probe expects 403 there
    assert not any(
        scope.split(".")[0] in {"diary", "accounts", "health"}
        for scope in verifier["scopes"]
    )


def test_x_feed_collector_reaches_only_the_x_feed():
    # An external bot (Grok, Chief Data) holds this token: no other intake source, no other cell.
    collector = next(a for a in REGISTRY["automations"] if a["id"] == "x-feed-collector")
    assert collector["scopes"]
    assert all(scope.startswith("intake.x-") for scope in collector["scopes"])
    assert not any(scope.endswith(":delete") for scope in collector["scopes"])
    # The lake export marks pulls through intake-api's x-feed routes (intake.x-sighting:write), so it
    # needs no intake.intake:* right, which would reach every other source's intake rows.
    exporter = next(a for a in REGISTRY["automations"] if a["id"] == "x-feed-lake-export")
    assert all(scope.startswith("intake.x-") for scope in exporter["scopes"])
    assert {s for s in exporter["scopes"] if s.endswith(":write")} == {"intake.x-sighting:write"}
    assert "intake.x-sighting:write" not in collector["scopes"]  # the bot cannot mark its own pulls exported
