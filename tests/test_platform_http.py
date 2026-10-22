"""Platform over HTTP for cells with their own database: decide, executions, graph events."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import pytest
from conftest import add_member, create_workspace, delegate
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_api.routes import register
from tasks_api.auth import TokenVerifier

from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import DECISIONS_OWNER

ISSUER = "empire-platform-test"


@dataclass
class World:
    ws: uuid.UUID
    other_ws: uuid.UUID
    owner: uuid.UUID
    other_owner: uuid.UUID
    partner: uuid.UUID
    publisher: uuid.UUID
    publish_delegation: uuid.UUID
    reader: uuid.UUID
    read_delegation: uuid.UUID


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("platform-keys"))


@pytest.fixture(scope="module")
def world(cluster) -> World:
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        ws = create_workspace(conn, f"cells-{suffix}", list(DECISIONS_OWNER))
        partner = add_member(conn, ws, "partner", "human")
        publisher = add_member(conn, ws, "decisions-publisher", "automation")
        reader = add_member(conn, ws, "decision-reader", "agent")
        publish_delegation = delegate(
            conn, ws, ws.owner, publisher, ["graph.decisions:write"]
        )
        read_delegation = delegate(
            conn, ws, ws.owner, reader, ["decisions.decision:read"]
        )
        other = create_workspace(conn, f"other-{suffix}", list(DECISIONS_OWNER))
        conn.commit()
    return World(
        ws.id,
        other.id,
        ws.owner,
        other.owner,
        partner,
        publisher,
        publish_delegation,
        reader,
        read_delegation,
    )


@pytest.fixture(scope="module")
def client(cluster, keys) -> TestClient:
    _, public_pem = keys
    app = FastAPI()
    register(
        app,
        connect_platform=lambda: cluster.connect("tasks_api"),
        platform_verifier=TokenVerifier(public_pem, ISSUER, audience="platform-api"),
        graph_verifier=TokenVerifier(public_pem, ISSUER, audience="graph-api"),
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


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        return conn.execute(query, params).fetchall()


def decide(client, headers, action="decisions.decision:read", resource_id="d-1"):
    return client.post(
        "/v1/authz/decide",
        headers=headers,
        json={
            "action": action,
            "resource_type": "decision",
            "resource_id": resource_id,
        },
    )


def test_decide_allows_the_owner_and_records_it(client, token, world, cluster):
    response = decide(client, token(world.owner, world.ws))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "allow"
    recorded = superuser(
        cluster,
        "SELECT decision, action FROM platform.authorization_decisions WHERE id = %s",
        (body["decision_id"],),
    )
    assert recorded == [("allow", "decisions.decision:read")]


def test_decide_denies_a_member_without_the_permission(client, token, world):
    assert decide(client, token(world.partner, world.ws)).json()["decision"] == "deny"


def test_delegated_reader_may_read_but_not_write(client, token, world):
    headers = token(
        world.reader, world.ws, delegation=world.read_delegation, obo=world.owner
    )
    assert decide(client, headers).json()["decision"] == "allow"
    assert (
        decide(client, headers, action="decisions.decision:write").json()["decision"]
        == "deny"
    )


def test_token_without_the_platform_audience_is_refused(client, token, world):
    assert (
        decide(client, token(world.owner, world.ws, audience=["tasks-api"])).status_code
        == 401
    )


def begin(client, headers, key=None, action="decisions.decision:write"):
    body = {"action": action, "resource_type": "decision", "resource_id": "d-1"}
    if key:
        body["idempotency_key"] = key
    return client.post("/v1/executions", headers=headers, json=body)


def finish(client, headers, execution_id, status="succeeded"):
    return client.post(
        f"/v1/executions/{execution_id}/finish",
        headers=headers,
        json={
            "status": status,
            "service": "decisions",
            "action": "decisions.decision.created",
            "resource_type": "decision",
            "resource_id": "d-1",
            "after_hash": hashlib.sha256(b"row").hexdigest(),
        },
    )


def test_write_execution_lifecycle_records_audit(client, token, world, cluster):
    headers = token(world.owner, world.ws)
    started = begin(client, headers)
    assert started.status_code == 201, started.text
    execution = started.json()["execution_id"]
    done = finish(client, headers, execution)
    assert done.status_code == 200
    assert done.json()["status"] == "succeeded"
    assert superuser(
        cluster, "SELECT status FROM platform.executions WHERE id = %s", (execution,)
    ) == [("succeeded",)]
    audit = superuser(
        cluster,
        "SELECT service, action, resource_id FROM platform.audit_events WHERE execution_id = %s",
        (execution,),
    )
    assert audit == [("decisions", "decisions.decision.created", "d-1")]
    again = finish(client, headers, execution)
    assert again.json()["replayed"] is True
    assert (
        len(
            superuser(
                cluster,
                "SELECT 1 FROM platform.audit_events WHERE execution_id = %s",
                (execution,),
            )
        )
        == 1
    )


def test_denied_write_starts_no_execution(client, token, world, cluster):
    denied = begin(client, token(world.partner, world.ws))
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "no_permission"
    rows = superuser(
        cluster,
        "SELECT count(*) FROM platform.executions WHERE executed_by = %s",
        (world.partner,),
    )
    assert rows == [(0,)]


def test_idempotency_key_replays_only_the_same_operation(client, token, world):
    key = f"decision-create-{uuid.uuid4().hex}"
    first = begin(client, token(world.owner, world.ws), key=key)
    second = begin(client, token(world.owner, world.ws), key=key)
    assert first.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["execution_id"] == first.json()["execution_id"]
    changed = begin(
        client,
        token(world.owner, world.ws),
        key=key,
        action="decisions.decision:delete",
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "idempotency_conflict"
    # Keys are per actor: another caller's key never reaches this execution.
    other = client.post(
        "/v1/executions",
        headers=token(world.partner, world.ws),
        json={
            "action": "decisions.decision:write",
            "resource_type": "decision",
            "resource_id": "d-1",
            "idempotency_key": key,
        },
    )
    assert other.status_code == 403


def test_read_delegation_cannot_start_or_replay_a_write(client, token, world):
    headers = token(
        world.reader, world.ws, delegation=world.read_delegation, obo=world.owner
    )
    assert begin(client, headers).status_code == 403
    assert begin(client, headers, action="decisions.decision:read").status_code == 422
    key = f"k-{uuid.uuid4().hex}"
    assert begin(client, headers, key=key).status_code == 403
    assert begin(client, headers, key=key).status_code == 403


def test_finish_audits_only_the_bound_operation(client, token, world, cluster):
    headers = token(world.owner, world.ws)
    execution = begin(client, headers).json()["execution_id"]

    def attempt(**overrides):
        body = {
            "status": "succeeded",
            "service": "decisions",
            "action": "decisions.decision.updated",
            "resource_type": "decision",
            "resource_id": "d-1",
            **overrides,
        }
        return client.post(
            f"/v1/executions/{execution}/finish", headers=headers, json=body
        )

    assert attempt(service="tasks", action="tasks.task.deleted").status_code == 409
    assert attempt(action="tasks.task.deleted").status_code == 409
    assert attempt(resource_id="d-2").status_code == 409
    assert attempt(resource_type="task").status_code == 409
    assert superuser(
        cluster,
        "SELECT count(*) FROM platform.audit_events WHERE execution_id = %s",
        (execution,),
    ) == [(0,)]
    assert attempt().status_code == 200


def test_audit_names_exactly_the_bound_resource_and_an_allowed_verb(
    client, token, world, cluster
):
    headers = token(world.owner, world.ws)
    started = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "action": "decisions.decision:write",
            "resource_type": "decision",
            "resource_id": "new",
        },
    )
    execution = started.json()["execution_id"]

    def attempt(**overrides):
        body = {
            "status": "succeeded",
            "service": "decisions",
            "action": "decisions.decision.created",
            "resource_type": "decision",
            "resource_id": "new",
            **overrides,
        }
        return client.post(
            f"/v1/executions/{execution}/finish", headers=headers, json=body
        )

    assert (
        attempt(resource_id="42").status_code == 409
    )  # no placeholders: the cell binds the real id
    assert (
        attempt(action="decisions.decision.deleted").status_code == 409
    )  # needs :delete
    assert (
        attempt(action="decisions.decision.approved").status_code == 409
    )  # needs :approve
    assert superuser(
        cluster,
        "SELECT count(*) FROM platform.audit_events WHERE execution_id = %s",
        (execution,),
    ) == [(0,)]
    assert attempt().status_code == 200


def test_finish_needs_the_authority_the_execution_started_with(
    client, token, world, cluster
):
    with cluster.connect("platform_api") as conn:
        from conftest import delegate

        writer = delegate(
            conn,
            type("W", (), {"id": world.ws})(),
            world.owner,
            world.reader,
            ["decisions.decision:read", "decisions.decision:write"],
        )
        conn.commit()
    write_headers = token(world.reader, world.ws, delegation=writer, obo=world.owner)
    read_headers = token(
        world.reader, world.ws, delegation=world.read_delegation, obo=world.owner
    )
    execution = begin(client, write_headers).json()["execution_id"]
    assert finish(client, read_headers, execution).status_code == 404
    assert finish(client, write_headers, execution).status_code == 200


def test_bad_execution_input_is_a_client_error(client, token, world):
    headers = token(world.owner, world.ws)
    body = {
        "action": "decisions.decision:write",
        "resource_type": "decision",
        "resource_id": "d-1",
    }
    assert (
        client.post(
            "/v1/executions", headers=headers, json={**body, "correlation_id": "x"}
        ).status_code
        == 422
    )
    missing_approval = client.post(
        "/v1/executions",
        headers=headers,
        json={**body, "approval_id": str(uuid.uuid4())},
    )
    assert missing_approval.status_code in (403, 409, 422)


def test_only_the_executing_actor_can_finish(client, token, world):
    execution = begin(client, token(world.owner, world.ws)).json()["execution_id"]
    assert finish(client, token(world.partner, world.ws), execution).status_code == 404
    assert (
        finish(client, token(world.other_owner, world.other_ws), execution).status_code
        == 404
    )


def event(ws, type_="decisions.decision.created", subject=None, payload=None, **extra):
    return {
        "id": str(uuid.uuid4()),
        "workspace_id": str(ws),
        "type": type_,
        "subject": subject or f"empire:decisions:decision:{uuid.uuid4()}",
        "version": 1,
        "payload": payload or {"version": 1, "title": "Pick a CRM"},
        "occurred_at": "2026-10-10T00:00:00+00:00",
        **extra,
    }


def publisher_headers(token, world, audience=None):
    kwargs = {"audience": audience} if audience else {}
    return token(
        world.publisher,
        world.ws,
        delegation=world.publish_delegation,
        obo=world.owner,
        **kwargs,
    )


def test_publisher_delivers_events_into_its_own_namespace(
    client, token, world, cluster
):
    payload = event(world.ws)
    response = client.post(
        "/v1/graph/events", headers=publisher_headers(token, world), json=payload
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] != "duplicate"
    replay = client.post(
        "/v1/graph/events", headers=publisher_headers(token, world), json=payload
    )
    assert replay.json()["outcome"] == "duplicate"
    nodes = superuser(
        cluster, "SELECT cell FROM graph.nodes WHERE urn = %s", (payload["subject"],)
    )
    assert nodes == [("decisions",)]


def test_publisher_cannot_write_another_cells_namespace(client, token, world):
    headers = publisher_headers(token, world)
    other_type = client.post(
        "/v1/graph/events",
        headers=headers,
        json=event(
            world.ws,
            type_="tasks.task.created",
            subject=f"empire:tasks:task:{uuid.uuid4()}",
        ),
    )
    assert other_type.status_code == 403
    other_subject = client.post(
        "/v1/graph/events",
        headers=headers,
        json=event(world.ws, subject=f"empire:tasks:task:{uuid.uuid4()}"),
    )
    assert other_subject.json()["detail"]["reason"] == "subject_outside_cell"


def test_alias_into_another_cell_is_refused(client, token, world, cluster):
    alias = f"empire:decisions:decision:{uuid.uuid4()}"
    target = f"empire:tasks:task:{uuid.uuid4()}"
    with cluster.connect("postgres") as conn:
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(world.ws),)
        )
        conn.execute(
            "INSERT INTO graph.aliases (alias_urn, urn, workspace_id) VALUES (%s, %s, %s)",
            (alias, target, world.ws),
        )
        conn.commit()
    response = client.post(
        "/v1/graph/events",
        headers=publisher_headers(token, world),
        json=event(world.ws, subject=alias),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "outside_cell"


def test_malformed_events_are_client_errors(client, token, world):
    headers = publisher_headers(token, world)
    linked = event(
        world.ws, type_="decisions.decision.linked", payload={"relation": "related-to"}
    )
    assert (
        client.post("/v1/graph/events", headers=headers, json=linked).status_code == 422
    )
    assert (
        client.post(
            "/v1/graph/events", headers=headers, json=event(world.ws, version=2**60)
        ).status_code
        == 422
    )


def test_events_for_another_workspace_or_without_permission_are_refused(
    client, token, world
):
    wrong_ws = client.post(
        "/v1/graph/events",
        headers=publisher_headers(token, world),
        json=event(world.other_ws),
    )
    assert wrong_ws.json()["detail"]["reason"] == "workspace_mismatch"
    partner = client.post(
        "/v1/graph/events", headers=token(world.partner, world.ws), json=event(world.ws)
    )
    assert partner.status_code == 403
    no_graph_audience = client.post(
        "/v1/graph/events",
        headers=publisher_headers(token, world, ["platform-api"]),
        json=event(world.ws),
    )
    assert no_graph_audience.status_code == 401


def test_every_permission_of_an_operation_is_enforced_at_begin(client, token, world):
    reader = token(
        world.reader, world.ws, delegation=world.read_delegation, obo=world.owner
    )
    both = {
        "action": "decisions.decision:approve",
        "also": ["decisions.decision:write"],
        "resource_type": "decision",
        "resource_id": "d-9",
    }
    assert client.post("/v1/executions", headers=reader, json=both).status_code == 403
    assert (
        client.post(
            "/v1/executions", headers=token(world.owner, world.ws), json=both
        ).status_code
        == 201
    )
    other_cell = {**both, "also": ["tasks.task:write"]}
    assert (
        client.post(
            "/v1/executions", headers=token(world.owner, world.ws), json=other_cell
        ).status_code
        == 422
    )


def test_audit_entity_must_match_the_permission(client, token, world):
    headers = token(world.owner, world.ws)
    execution = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "action": "decisions.review:write",
            "resource_type": "decision",
            "resource_id": "d-1",
        },
    ).json()["execution_id"]
    body = {
        "status": "succeeded",
        "service": "decisions",
        "resource_type": "decision",
        "resource_id": "d-1",
    }
    forged = client.post(
        f"/v1/executions/{execution}/finish",
        headers=headers,
        json={**body, "action": "decisions.decision.created"},
    )
    assert forged.status_code == 409
    assert (
        client.post(
            f"/v1/executions/{execution}/finish",
            headers=headers,
            json={**body, "action": "decisions.review.created"},
        ).status_code
        == 200
    )
