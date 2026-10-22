"""Tasks API integration tests: human, agent, and cross-workspace callers."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
import psycopg
import pytest
from conftest import add_member, create_workspace, delegate, new_id, use_workspace
from fastapi.testclient import TestClient
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER

ISSUER = "empire-platform-test"


@dataclass
class World:
    alpha: uuid.UUID
    beta: uuid.UUID
    owner: uuid.UUID
    partner: uuid.UUID
    coach: uuid.UUID
    outsider: uuid.UUID
    coach_read: uuid.UUID
    coach_write: uuid.UUID
    coach_revoked: uuid.UUID
    delete_approved: uuid.UUID


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("keys"))


@pytest.fixture(scope="module")
def world(cluster) -> World:
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        alpha = create_workspace(conn, f"alpha-{suffix}", list(TASKS_OWNER))
        partner = add_member(conn, alpha, "partner", "human")
        coach = add_member(conn, alpha, "coach", "agent")
        use_workspace(conn, alpha.id)
        reader = new_id(conn)
        conn.execute(
            "INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, 'reader')",
            (reader, alpha.id),
        )
        conn.execute(
            "INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, 'tasks.task:read')",
            (alpha.id, reader),
        )
        conn.execute(
            "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
            (alpha.id, partner, reader),
        )
        coach_read = delegate(conn, alpha, alpha.owner, coach, ["tasks.task:read"])
        coach_write = delegate(
            conn, alpha, alpha.owner, coach, ["tasks.task:read", "tasks.task:write"]
        )
        coach_revoked = delegate(
            conn, alpha, alpha.owner, coach, ["tasks.task:read", "tasks.task:write"]
        )
        conn.execute(
            "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
            (coach_revoked,),
        )
        conn.execute(
            "INSERT INTO platform.policies (workspace_id, permission, requires_approval) VALUES (%s, 'tasks.task:delete', true)",
            (alpha.id,),
        )
        approval = new_id(conn)
        conn.execute(
            "INSERT INTO platform.approvals (id, workspace_id, requested_action, approver_identity_id, decision, decided_at)"
            " VALUES (%s, %s, 'tasks.task:delete', %s, 'approved', now())",
            (approval, alpha.id, alpha.owner),
        )
        beta = create_workspace(conn, f"beta-{suffix}", list(TASKS_OWNER))
        conn.commit()
    return World(
        alpha.id,
        beta.id,
        alpha.owner,
        partner,
        coach,
        beta.owner,
        coach_read,
        coach_write,
        coach_revoked,
        approval,
    )


@pytest.fixture(scope="module")
def client(cluster, keys) -> TestClient:
    _, public_pem = keys
    app = create_app(
        lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)
    )
    return TestClient(app)


@pytest.fixture
def token(keys):
    private_pem, _ = keys

    def make(sub, ws, **kwargs) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {mint(private_pem, sub=sub, ws=ws, issuer=ISSUER, **kwargs)}"
        }

    return make


def create(client, headers, title="Register for the marathon", **extra):
    response = client.post("/v1/tasks", json={"title": title, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        return conn.execute(query, params).fetchall()


# --- human owner ---------------------------------------------------------------


def test_owner_lifecycle_with_outbox_and_audit(client, token, world, cluster):
    owner = token(world.owner, world.alpha)
    task = create(client, owner, priority=2, tags=["running"])
    assert task["urn"] == f"empire:tasks:task:{task['id']}"
    assert (
        client.get(f"/v1/tasks/{task['id']}", headers=owner).json()["title"]
        == "Register for the marathon"
    )
    assert task["id"] in [
        item["id"] for item in client.get("/v1/tasks", headers=owner).json()["items"]
    ]

    updated = client.patch(
        f"/v1/tasks/{task['id']}",
        json={"status": "in_progress"},
        headers={**owner, "If-Match": str(task["version"])},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == task["version"] + 1
    stale = client.patch(
        f"/v1/tasks/{task['id']}",
        json={"priority": 1},
        headers={**owner, "If-Match": str(task["version"])},
    )
    assert stale.status_code == 412

    done = client.post(f"/v1/tasks/{task['id']}/complete", headers=owner).json()
    assert done["status"] == "done"
    assert done["completed_at"] is not None
    link = client.post(
        f"/v1/tasks/{task['id']}/links",
        headers=owner,
        json={
            "relation": "supports-goal",
            "target_type": "goal",
            "target_ref": "empire:goals:goal:marathon",
        },
    )
    assert link.status_code == 201

    events = superuser(
        cluster,
        "SELECT type FROM tasks.outbox WHERE subject = %s ORDER BY occurred_at",
        (task["urn"],),
    )
    assert [e[0] for e in events] == [
        "tasks.task.created",
        "tasks.task.updated",
        "tasks.task.completed",
        "tasks.task.linked",
    ]
    audit = superuser(
        cluster,
        "SELECT count(*) FROM platform.audit_events WHERE resource_id = %s",
        (task["id"],),
    )[0][0]
    assert audit == 4
    executions = superuser(
        cluster,
        "SELECT DISTINCT e.initiated_by, e.executed_by, e.on_behalf_of, e.status FROM tasks.outbox o"
        " JOIN platform.executions e ON e.id = o.execution_id WHERE o.subject = %s",
        (task["urn"],),
    )
    assert executions == [(world.owner, world.owner, world.owner, "succeeded")]


def test_delete_needs_an_approval(client, token, world):
    owner = token(world.owner, world.alpha)
    task = create(client, owner, title="Old plan")
    blocked = client.delete(f"/v1/tasks/{task['id']}", headers=owner)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "approval_required"
    approved = client.delete(
        f"/v1/tasks/{task['id']}",
        headers={**owner, "X-Empire-Approval": str(world.delete_approved)},
    )
    assert approved.status_code == 204
    assert client.get(f"/v1/tasks/{task['id']}", headers=owner).status_code == 404


@pytest.mark.parametrize("delegated", [False, True])
def test_idempotent_create(client, token, world, cluster, delegated):
    caller = (
        token(world.coach, world.alpha, delegation=world.coach_write, obo=world.owner)
        if delegated
        else token(world.owner, world.alpha)
    )
    headers = {**caller, "Idempotency-Key": f"create-{uuid.uuid4()}"}
    first = client.post("/v1/tasks", json={"title": "Buy shoes"}, headers=headers)
    second = client.post("/v1/tasks", json={"title": "Buy shoes"}, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    events = superuser(
        cluster,
        "SELECT count(*) FROM tasks.outbox WHERE subject = %s",
        (first.json()["urn"],),
    )[0][0]
    assert events == 1


@pytest.mark.parametrize(
    "caller_kind", ["write_only_member", "revoked_owner_read", "revoked_delegated_read"]
)
def test_idempotent_create_replay_requires_current_read(
    client, token, cluster, race, caller_kind
):
    ws, agent, delegation = race
    owner = token(ws.owner, ws.id)
    delegated = token(agent, ws.id, delegation=delegation, obo=ws.owner)
    key = {"Idempotency-Key": f"read-guard-{uuid.uuid4()}"}
    first = client.post(
        "/v1/tasks",
        json={"title": "Synthetic confidential task", "notes": "Synthetic notes"},
        headers={
            **(delegated if caller_kind == "revoked_delegated_read" else owner),
            **key,
        },
    )
    assert first.status_code == 201
    task = first.json()
    with cluster.connect("platform_api") as conn:
        use_workspace(conn, ws.id)
        if caller_kind == "write_only_member":
            actor = add_member(conn, ws, "writer", "human")
            role = new_id(conn)
            conn.execute(
                "INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, 'writer')",
                (role, ws.id),
            )
            conn.execute(
                "INSERT INTO platform.role_permissions (workspace_id, role_id, permission)"
                " VALUES (%s, %s, 'tasks.task:write')",
                (ws.id, role),
            )
            conn.execute(
                "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id)"
                " VALUES (%s, %s, %s)",
                (ws.id, actor, role),
            )
            caller = token(actor, ws.id)
        elif caller_kind == "revoked_owner_read":
            actor, caller = ws.owner, owner
            conn.execute(
                "DELETE FROM platform.role_permissions"
                " WHERE role_id = %s AND permission = 'tasks.task:read'",
                (ws.owner_role,),
            )
        else:
            actor, caller = agent, delegated
            conn.execute(
                "UPDATE platform.delegations SET scopes = ARRAY['tasks.task:write']::platform.permission[]"
                " WHERE id = %s",
                (delegation,),
            )
        conn.commit()
    assert client.get(f"/v1/tasks/{task['id']}", headers=caller).status_code == 404
    denial_query = (
        "SELECT count(*) FROM platform.authorization_decisions WHERE actor_identity_id = %s"
        " AND action = 'tasks.task:read' AND resource_id = %s AND decision = 'deny'"
    )
    before = superuser(cluster, denial_query, (actor, task["id"]))[0][0]
    replay = client.post(
        "/v1/tasks", json={"title": "Retry"}, headers={**caller, **key}
    )
    assert replay.status_code == 404
    assert replay.json() == {"detail": {"error": "not_found"}}
    assert superuser(cluster, denial_query, (actor, task["id"]))[0][0] == before + 1
    assert (
        superuser(
            cluster,
            "SELECT count(*) FROM tasks.outbox WHERE subject = %s",
            (task["urn"],),
        )[0][0]
        == 1
    )


@pytest.mark.parametrize("delegated", [False, True])
def test_idempotent_create_replay_requires_token_read_scope(
    client, token, cluster, keys, race, delegated
):
    ws, agent, delegation = race
    caller = (
        token(agent, ws.id, delegation=delegation, obo=ws.owner)
        if delegated
        else token(ws.owner, ws.id)
    )
    private_pem, public_pem = keys
    claims = jwt.decode(
        caller["Authorization"].removeprefix("Bearer "),
        public_pem,
        algorithms=["RS256"],
        audience="tasks-api",
        issuer=ISSUER,
    )
    claims["scope"] = "tasks.task:write"
    headers = {
        "Authorization": f"Bearer {jwt.encode(claims, private_pem, algorithm='RS256')}",
        "Idempotency-Key": f"scoped-replay-{uuid.uuid4()}",
    }
    first = client.post(
        "/v1/tasks", json={"title": "Write-only scoped create"}, headers=headers
    )
    assert first.status_code == 201
    task = first.json()
    assert client.get(f"/v1/tasks/{task['id']}", headers=headers).status_code == 404
    replay = client.post("/v1/tasks", json={"title": "Retry"}, headers=headers)
    assert replay.status_code == 404
    assert replay.json() == {"detail": {"error": "not_found"}}
    assert (
        superuser(
            cluster,
            "SELECT count(*) FROM tasks.outbox WHERE subject = %s",
            (task["urn"],),
        )[0][0]
        == 1
    )


def test_idempotent_create_key_is_workspace_bound(client, token, world, cluster):
    key = {"Idempotency-Key": f"workspace-replay-{uuid.uuid4()}"}
    alpha = {**token(world.owner, world.alpha), **key}
    beta = {**token(world.outsider, world.beta), **key}
    first = client.post(
        "/v1/tasks", json={"title": "Alpha synthetic task"}, headers=alpha
    )
    other = client.post(
        "/v1/tasks", json={"title": "Beta synthetic task"}, headers=beta
    )
    assert (first.status_code, other.status_code) == (201, 201)
    assert first.json()["id"] != other.json()["id"]
    for headers, created in ((alpha, first), (beta, other)):
        replay = client.post("/v1/tasks", json={"title": "Retry"}, headers=headers)
        assert replay.status_code == 200
        assert replay.json() == created.json()
        assert (
            superuser(
                cluster,
                "SELECT count(*) FROM tasks.outbox WHERE subject = %s",
                (created.json()["urn"],),
            )[0][0]
            == 1
        )


def test_read_only_member_cannot_write(client, token, world):
    owner = token(world.owner, world.alpha)
    task = create(client, owner)
    partner = token(world.partner, world.alpha)
    assert client.get(f"/v1/tasks/{task['id']}", headers=partner).status_code == 200
    denied = client.patch(
        f"/v1/tasks/{task['id']}", json={"priority": 1}, headers=partner
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "no_permission"
    assert (
        client.post("/v1/tasks", json={"title": "x"}, headers=partner).status_code
        == 403
    )


# --- agents ----------------------------------------------------------------------


def test_agent_with_read_delegation(client, token, world):
    task = create(client, token(world.owner, world.alpha))
    coach = token(
        world.coach,
        world.alpha,
        delegation=world.coach_read,
        obo=world.owner,
        act=world.owner,
    )
    assert client.get(f"/v1/tasks/{task['id']}", headers=coach).status_code == 200
    assert client.get("/v1/tasks", headers=coach).status_code == 200
    denied = client.post("/v1/tasks", json={"title": "Run 20 km"}, headers=coach)
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "no_permission"


def test_agent_write_is_attributed_to_the_delegation(client, token, world, cluster):
    coach = {
        **token(
            world.coach,
            world.alpha,
            delegation=world.coach_write,
            obo=world.owner,
            act=world.owner,
        ),
        "X-Empire-Channel": "mcp",
    }
    task = create(client, coach, title="Long run 20 km")
    row = superuser(
        cluster,
        "SELECT e.initiated_by, e.executed_by, e.on_behalf_of, e.delegation_id, e.channel FROM tasks.tasks t"
        " JOIN platform.executions e ON e.id = t.created_by_execution WHERE t.id = %s",
        (task["id"],),
    )[0]
    assert row == (world.owner, world.coach, world.owner, world.coach_write, "mcp")


def test_agent_without_delegation_learns_nothing(client, token, world):
    task = create(client, token(world.owner, world.alpha))
    coach = token(world.coach, world.alpha)
    assert client.get(f"/v1/tasks/{task['id']}", headers=coach).status_code == 404
    assert client.get("/v1/tasks", headers=coach).status_code == 403
    assert (
        client.patch(
            f"/v1/tasks/{task['id']}", json={"priority": 1}, headers=coach
        ).status_code
        == 404
    )


def test_revoked_delegation_is_denied_and_recorded(client, token, world, cluster):
    coach = token(
        world.coach, world.alpha, delegation=world.coach_revoked, obo=world.owner
    )
    response = client.post("/v1/tasks", json={"title": "x"}, headers=coach)
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "delegation_invalid"
    recorded = superuser(
        cluster,
        "SELECT count(*) FROM platform.authorization_decisions WHERE delegation_id = %s AND reason = 'delegation_invalid'",
        (world.coach_revoked,),
    )[0][0]
    assert recorded >= 1


def test_agent_cannot_claim_to_act_for_someone_else(client, token, world):
    coach = token(
        world.coach, world.alpha, delegation=world.coach_write, obo=world.partner
    )
    response = client.post("/v1/tasks", json={"title": "x"}, headers=coach)
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "delegation_invalid"


# --- cross-workspace ----------------------------------------------------------------


def test_other_workspace_sees_nothing(client, token, world):
    task = create(client, token(world.owner, world.alpha), title="Alpha secret")
    outsider = token(world.outsider, world.beta)
    assert client.get(f"/v1/tasks/{task['id']}", headers=outsider).status_code == 404
    assert (
        client.patch(
            f"/v1/tasks/{task['id']}", json={"title": "pwned"}, headers=outsider
        ).status_code
        == 404
    )
    assert (
        client.post(f"/v1/tasks/{task['id']}/complete", headers=outsider).status_code
        == 404
    )
    assert task["id"] not in [
        item["id"] for item in client.get("/v1/tasks", headers=outsider).json()["items"]
    ]


def test_non_member_token_for_a_workspace_is_refused(client, token, world):
    task = create(client, token(world.owner, world.alpha))
    forged = token(
        world.outsider, world.alpha
    )  # validly signed, but not a member of alpha
    assert client.get("/v1/tasks", headers=forged).status_code == 403
    assert client.get(f"/v1/tasks/{task['id']}", headers=forged).status_code == 404


# --- authentication -------------------------------------------------------------------


def test_bad_tokens_are_rejected(client, token, world, keys):
    assert client.get("/v1/tasks").status_code == 401
    other_private, _ = key_pair_for_test()
    forged = mint(other_private, sub=world.owner, ws=world.alpha, issuer=ISSUER)
    assert (
        client.get(
            "/v1/tasks", headers={"Authorization": f"Bearer {forged}"}
        ).status_code
        == 401
    )
    private_pem, _ = keys
    too_long = mint(
        private_pem, sub=world.owner, ws=world.alpha, issuer=ISSUER, ttl=7200
    )
    assert (
        client.get(
            "/v1/tasks", headers={"Authorization": f"Bearer {too_long}"}
        ).status_code
        == 401
    )
    half = mint(
        private_pem,
        sub=world.coach,
        ws=world.alpha,
        issuer=ISSUER,
        delegation=world.coach_read,
    )
    assert (
        client.get("/v1/tasks", headers={"Authorization": f"Bearer {half}"}).status_code
        == 401
    )
    now = int(time.time())
    expired = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "tasks-api",
            "sub": str(world.owner),
            "ws": str(world.alpha),
            "iat": now - 600,
            "exp": now - 1,
            "jti": "x",
        },
        private_pem,
        algorithm="RS256",
    )
    assert (
        client.get(
            "/v1/tasks", headers={"Authorization": f"Bearer {expired}"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/tasks",
            headers={**token(world.owner, world.alpha), "X-Empire-Channel": "email"},
        ).status_code
        == 401
    )


def key_pair_for_test():
    import tempfile
    from pathlib import Path

    return key_pair(Path(tempfile.mkdtemp()))


# --- database role -----------------------------------------------------------------------


def test_tasks_role_cannot_read_platform_tables_or_bypass_rls(cluster):
    with cluster.connect("tasks_api") as conn:
        for table in (
            "platform.memberships",
            "platform.delegations",
            "platform.audit_events",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(f"SELECT count(*) FROM {table}")
        assert (
            conn.execute("SELECT count(*) FROM tasks.tasks").fetchone()[0] == 0
        )  # no workspace bound
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("ALTER TABLE tasks.tasks DISABLE ROW LEVEL SECURITY")
        attributes = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'tasks_api'"
        ).fetchone()
        assert attributes == (False, False)


# --- time of check vs time of use ---------------------------------------------------------


@pytest.fixture
def race(cluster):
    """A separate workspace whose owner delegates read and write to an agent."""
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn,
            f"race-{uuid.uuid4().hex[:8]}",
            ["tasks.task:read", "tasks.task:write"],
        )
        agent = add_member(conn, ws, "racer", "agent")
        delegation = delegate(
            conn, ws, ws.owner, agent, ["tasks.task:read", "tasks.task:write"]
        )
        conn.commit()
    return ws, agent, delegation


def revoke_after_precheck(monkeypatch, cluster, workspace_id, statement, params):
    """Run ``statement`` (committed) right after the unlocked precheck allows a write."""
    import tasks_api.app as app_module

    original = app_module.authorize
    state = {"done": False}

    def authorize_then_change(conn, request):
        decision = original(conn, request)
        if (
            decision.allowed
            and request.action == "tasks.task:write"
            and not state["done"]
        ):
            with cluster.connect("platform_api") as owner:
                use_workspace(owner, workspace_id)
                owner.execute(statement, params)
                owner.commit()
            state["done"] = True
        return decision

    monkeypatch.setattr(app_module, "authorize", authorize_then_change)
    return state


def test_idempotent_create_replay_rechecks_live_delegation(
    client, token, cluster, race, monkeypatch
):
    ws, agent, delegation = race
    headers = {
        **token(agent, ws.id, delegation=delegation, obo=ws.owner),
        "Idempotency-Key": f"revoked-replay-{uuid.uuid4()}",
    }
    first = client.post(
        "/v1/tasks", json={"title": "Before revocation"}, headers=headers
    )
    assert first.status_code == 201
    state = revoke_after_precheck(
        monkeypatch,
        cluster,
        ws.id,
        "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
        (delegation,),
    )
    replay = client.post("/v1/tasks", json={"title": "Retry"}, headers=headers)
    assert state["done"]
    assert replay.status_code == 404
    assert replay.json() == {"detail": {"error": "not_found"}}
    denied = superuser(
        cluster,
        "SELECT count(*) FROM platform.authorization_decisions WHERE delegation_id = %s"
        " AND action = 'tasks.task:read' AND resource_id = %s AND reason = 'delegation_invalid'",
        (delegation, first.json()["id"]),
    )[0][0]
    assert denied == 1
    assert (
        superuser(
            cluster,
            "SELECT count(*) FROM tasks.outbox WHERE subject = %s",
            (first.json()["urn"],),
        )[0][0]
        == 1
    )


def test_grantor_losing_permission_after_precheck_is_enforced(
    client, token, cluster, race, monkeypatch
):
    """Only the locked re-check (enforce) in the write transaction can catch this."""
    ws, agent, delegation = race
    state = revoke_after_precheck(
        monkeypatch,
        cluster,
        ws.id,
        "DELETE FROM platform.role_permissions WHERE role_id = %s AND permission = 'tasks.task:write'",
        (ws.owner_role,),
    )
    response = client.post(
        "/v1/tasks",
        json={"title": "Raced by grantor"},
        headers=token(agent, ws.id, delegation=delegation, obo=ws.owner),
    )
    assert state["done"]
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "no_permission"
    assert (
        superuser(
            cluster, "SELECT count(*) FROM tasks.tasks WHERE title = 'Raced by grantor'"
        )[0][0]
        == 0
    )


def test_delegation_revoked_after_precheck_is_refused(
    client, token, cluster, race, monkeypatch
):
    ws, agent, delegation = race
    state = revoke_after_precheck(
        monkeypatch,
        cluster,
        ws.id,
        "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
        (delegation,),
    )
    response = client.post(
        "/v1/tasks",
        json={"title": "Raced by revocation"},
        headers=token(agent, ws.id, delegation=delegation, obo=ws.owner),
    )
    assert state["done"]
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "delegation_invalid"
    assert (
        superuser(
            cluster,
            "SELECT count(*) FROM tasks.tasks WHERE title = 'Raced by revocation'",
        )[0][0]
        == 0
    )
