"""Shared policy decision point: conformance cases, TOCTOU, and audit trail."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from conftest import add_member, create_workspace, delegate, new_id, use_workspace
from empire_authz import AuthorizationDenied, Request, Resource, authorize, enforce

CONFORMANCE = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "authz"
    / "v1"
    / "conformance.json"
)
SPEC = json.loads(CONFORMANCE.read_text(encoding="utf-8"))


def build_world(conn) -> dict[str, uuid.UUID]:
    """Create the conformance world; returns name -> id for every named object."""
    world = SPEC["world"]
    names: dict[str, uuid.UUID] = {}
    workspaces = {}
    for slug, spec in world["workspaces"].items():
        ws = create_workspace(
            conn, f"{slug}-{uuid.uuid4().hex[:8]}", spec["owner_permissions"]
        )
        workspaces[slug] = ws
        names[slug] = ws.id
        names[spec["owner"]] = ws.owner
    for slug, members in world["members"].items():
        for name, kind in members.items():
            names[name] = add_member(conn, workspaces[slug], name, kind)
    for name, spec in world["delegations"].items():
        ws = workspaces[spec["workspace"]]
        expires = timedelta(seconds=-1) if spec.get("expired") else timedelta(hours=1)
        names[name] = delegate(
            conn,
            ws,
            names[spec["grantor"]],
            names[spec["grantee"]],
            spec["scopes"],
            expires,
        )
        if spec.get("revoked"):
            conn.execute(
                "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
                (names[name],),
            )
    for slug, policies in world["policies"].items():
        use_workspace(conn, workspaces[slug].id)
        for policy in policies:
            conn.execute(
                "INSERT INTO platform.policies (workspace_id, permission, requires_approval) VALUES (%s, %s, %s)",
                (
                    workspaces[slug].id,
                    policy["permission"],
                    policy["requires_approval"],
                ),
            )
    for name, spec in world["approvals"].items():
        ws = workspaces[spec["workspace"]]
        use_workspace(conn, ws.id)
        names[name] = new_id(conn)
        decided = spec["decision"] != "pending"
        conn.execute(
            "INSERT INTO platform.approvals (id, workspace_id, requested_action, approver_identity_id, decision, decided_at)"
            " VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)",
            (
                names[name],
                ws.id,
                spec["action"],
                ws.owner if decided else None,
                spec["decision"],
                decided,
            ),
        )
    return names


def request_from(
    case: dict, names: dict[str, uuid.UUID], *, channel: str = "api"
) -> Request:
    spec = case["request"]
    return Request(
        actor=names[spec["actor"]],
        workspace=names[spec["workspace"]],
        action=spec["action"],
        resource=Resource("task", "*", names[spec["resource_workspace"]]),
        channel=channel,
        delegation=names.get(spec.get("delegation", "")),
        on_behalf_of=names.get(spec.get("on_behalf_of", "")),
        approval=names.get(spec.get("approval", "")),
    )


@pytest.mark.parametrize(
    "case", SPEC["cases"], ids=[case["name"] for case in SPEC["cases"]]
)
def test_conformance(api, case):
    names = build_world(api)
    use_workspace(api, names[case["bound_to"]])
    decision = authorize(api, request_from(case, names))
    assert [decision.decision, decision.reason] == case["expect"]


def test_every_decision_is_recorded_in_its_workspace(api):
    names = build_world(api)
    use_workspace(api, names["alpha"])
    before = api.execute(
        "SELECT count(*) FROM platform.authorization_decisions"
    ).fetchone()[0]
    case = next(
        c for c in SPEC["cases"] if c["name"] == "agent beyond a read delegation"
    )
    decision = authorize(api, request_from(case, names, channel="mcp"))
    row = api.execute(
        "SELECT actor_identity_id, action, decision, reason, channel, delegation_id, locked"
        " FROM platform.authorization_decisions WHERE id = %s",
        (decision.decision_id,),
    ).fetchone()
    assert row == (
        names["coach"],
        "tasks.task:write",
        "deny",
        "no_permission",
        "mcp",
        names["coach_read"],
        False,
    )
    assert (
        api.execute("SELECT count(*) FROM platform.authorization_decisions").fetchone()[
            0
        ]
        == before + 1
    )
    use_workspace(api, names["beta"])
    assert (
        api.execute(
            "SELECT count(*) FROM platform.authorization_decisions WHERE id = %s",
            (decision.decision_id,),
        ).fetchone()[0]
        == 0
    )


def test_decision_records_are_append_only(api):
    names = build_world(api)
    use_workspace(api, names["alpha"])
    authorize(api, request_from(SPEC["cases"][0], names))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), api.transaction():
        api.execute("DELETE FROM platform.authorization_decisions")


def test_enforce_raises_unless_allowed(api):
    names = build_world(api)
    use_workspace(api, names["alpha"])
    case = next(c for c in SPEC["cases"] if c["name"] == "policy requires approval")
    with pytest.raises(AuthorizationDenied) as denied:
        enforce(api, request_from(case, names))
    assert denied.value.decision.decision == "require_approval"


def committed_world(cluster) -> dict[str, uuid.UUID]:
    with cluster.connect("platform_api") as conn:
        names = build_world(conn)
        conn.commit()
    return names


def coach_read(names) -> Request:
    return Request(
        names["coach"],
        names["alpha"],
        "tasks.task:read",
        Resource("task", "*", names["alpha"]),
        channel="workflow",
        delegation=names["coach_read"],
        on_behalf_of=names["owner"],
    )


def test_revocation_after_queueing_is_seen_when_the_worker_acts(cluster):
    """TOCTOU: allowed when queued, revoked, then denied at execution time."""
    names = committed_world(cluster)
    with cluster.connect("platform_api") as queue:
        use_workspace(queue, names["alpha"])
        assert authorize(queue, coach_read(names)).allowed
        queue.commit()
    with cluster.connect("platform_api") as owner:
        use_workspace(owner, names["alpha"])
        owner.execute(
            "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
            (names["coach_read"],),
        )
        owner.commit()
    with cluster.connect("platform_api") as worker:
        use_workspace(worker, names["alpha"])
        with pytest.raises(AuthorizationDenied) as denied:
            enforce(worker, coach_read(names))
        assert denied.value.decision.reason == "delegation_invalid"


def test_revocation_waits_for_an_acting_transaction(cluster):
    """TOCTOU: a revocation issued mid-action waits until the action commits."""
    names = committed_world(cluster)
    worker = cluster.connect("platform_api")
    use_workspace(worker, names["alpha"])
    enforce(worker, coach_read(names))  # locks the delegation row FOR SHARE

    revoked_at: dict[str, float] = {}

    def revoke() -> None:
        with cluster.connect("platform_api") as owner:
            use_workspace(owner, names["alpha"])
            owner.execute(
                "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
                (names["coach_read"],),
            )
            owner.commit()
            revoked_at["t"] = time.monotonic()

    thread = threading.Thread(target=revoke)
    thread.start()
    time.sleep(0.5)
    assert "t" not in revoked_at, "revocation must wait for the acting transaction"
    committed_at = time.monotonic()
    worker.commit()
    worker.close()
    thread.join(timeout=10)
    assert revoked_at["t"] >= committed_at
