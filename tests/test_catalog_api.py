"""SPEC-002 catalog over HTTP (tasks-api routes) and MCP (read-only tools, both adapters share build_server)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import jwt
import pytest
from conftest import add_member, create_workspace, new_id, use_workspace
from empire_mcp.server import build_server
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier
from test_catalog import CATALOG, READS, add_role, seed, su

from empire_platform.devtoken import key_pair
from empire_platform.token_exchange import MCP_SCOPES

ISSUER = "catalog-api-test"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("catalog-keys"))


@pytest.fixture(scope="module")
def client(cluster, keys) -> TestClient:
    _, public_pem = keys
    return TestClient(
        create_app(
            lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)
        )
    )


@pytest.fixture
def auth(keys):
    private_pem, _ = keys

    def make(sub, ws, scopes: list[str] | None = None) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "tasks-api",
            "sub": str(sub),
            "ws": str(ws),
            "iat": now,
            "exp": now + 600,
            "jti": str(uuid.uuid4()),
        }
        if scopes is not None:
            claims["scope"] = " ".join(scopes)
        return {
            "Authorization": "Bearer "
            + jwt.encode(claims, private_pem, algorithm="RS256")
        }

    return make


@pytest.fixture
def ws(cluster):
    with cluster.connect("platform_api") as conn:
        made = create_workspace(conn, f"capi-{uuid.uuid4().hex[:8]}", CATALOG + READS)
        conn.commit()
    seed(cluster, made.id)
    return made


def test_concept_lifecycle_over_http(client, auth, ws):  # AC-1, AC-13, AC-28
    owner = auth(ws.owner, ws.id)
    created = client.post(
        "/v1/catalog/concepts",
        headers=owner,
        json={
            "kind": "domain",
            "slug": "travel",
            "label": "การเดินทาง",
            "note": "new domain",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["urn"] == f"empire:platform:domain:{ws.id}:travel"
    dup = client.post(
        "/v1/catalog/concepts",
        headers=owner,
        json={"kind": "domain", "slug": "travel", "label": "x", "note": "again"},
    )
    assert (dup.status_code, dup.json()["detail"]["error"]) == (409, "exists")
    patched = client.patch(
        "/v1/catalog/concepts/domain/travel", headers=owner, json={"label": "Travel"}
    )
    assert patched.json()["label"] == "Travel"
    merged = client.post(
        "/v1/catalog/concepts/domain/travel/merge",
        headers=owner,
        json={"into": "experience", "note": "travel is experience"},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["concept"]["was"] == [f"empire:platform:domain:{ws.id}:travel"]
    domains = client.get(
        "/v1/catalog/concepts",
        headers=owner,
        params={"kind": "domain", "status": "active"},
    )
    assert "travel" not in {c["slug"] for c in domains.json()["items"]}
    versions = client.get("/v1/catalog/versions", headers=owner).json()["items"]
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert all(v["restricted_excluded"] for v in versions)


def test_dependencies_and_moves_over_http(client, auth, ws):  # AC-12
    owner = auth(ws.owner, ws.id)
    refused = client.post(
        "/v1/catalog/concepts/domain/work/retire", headers=owner, json={"note": "drop"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "error": "metrics_depend",
        "metrics": ["time.work_minutes"],
        "hidden_dependencies": 0,
    }
    done = client.post(
        "/v1/catalog/concepts/domain/work/retire",
        headers=owner,
        json={"note": "drop", "metric_moves": {"time.work_minutes": "skills"}},
    )
    assert done.status_code == 200, done.text
    assert done.json()["moved_metrics"] == ["time.work_minutes"]
    split = client.post(
        "/v1/catalog/concepts/domain/finance/split",
        headers=owner,
        json={"into": ["skills"], "note": "one target"},
    )
    assert split.status_code == 422  # the route model needs two targets


def test_scopes_are_checked_before_the_database(client, auth, ws):  # AC-39
    narrow = auth(ws.owner, ws.id, ["tasks.task:read"])
    assert client.get("/v1/catalog/concepts", headers=narrow).status_code == 404
    assert client.get("/v1/catalog/review", headers=narrow).status_code == 404
    write = client.post(
        "/v1/catalog/concepts",
        headers=narrow,
        json={"kind": "value", "slug": "calm", "label": "Calm", "note": "x"},
    )
    assert (write.status_code, write.json()["detail"]["reason"]) == (
        403,
        "scope_insufficient",
    )
    metrics_only = auth(ws.owner, ws.id, ["catalog.metric-definition:read"])
    assert (
        client.get("/v1/catalog/metrics/diary.energy", headers=metrics_only).status_code
        == 404
    )
    keys = {
        m["key"]
        for m in client.get("/v1/catalog/metrics", headers=metrics_only).json()["items"]
    }
    assert keys == {"time.learning_minutes", "time.work_minutes"}
    assert (
        client.get(
            "/v1/catalog/metrics/diary.energy", headers=auth(ws.owner, ws.id)
        ).status_code
        == 200
    )


def test_metric_put_over_http(client, auth, ws):  # AC-15, AC-17, AC-51
    owner = auth(ws.owner, ws.id)
    current = client.get("/v1/catalog/metrics/time.work_minutes", headers=owner).json()
    body = {
        k: current[k]
        for k in (
            "label",
            "description",
            "domain",
            "unit",
            "scale",
            "value_range",
            "kind",
            "objectivity",
            "source",
            "aggregation",
            "cadence",
            "criteria",
            "target",
        )
    }
    stale = client.put(
        "/v1/catalog/metrics/time.work_minutes", headers=owner, json=body
    )
    assert (stale.status_code, stale.json()["detail"]["error"]) == (
        409,
        "version_conflict",
    )
    same = client.put(
        "/v1/catalog/metrics/time.work_minutes",
        headers=owner,
        json={**body, "if_version": current["version"]},
    )
    assert same.status_code == 200, same.text
    assert same.json()["version"] == current["version"]
    bad = client.put(
        "/v1/catalog/metrics/time.work_minutes",
        headers=owner,
        json={
            **body,
            "aggregation": "mean",
            "scale": "ordinal",
            "if_version": current["version"],
        },
    )
    assert bad.status_code == 422
    assert (
        client.put("/v1/catalog/metrics/Bad-Key", headers=owner, json=body).status_code
        == 422
    )


def test_approval_required_is_409_and_recorded(cluster, client, auth, ws):  # AC-25
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
    owner = auth(ws.owner, ws.id)
    body = {"into": "experience", "note": "fold"}
    waiting = client.post(
        "/v1/catalog/concepts/domain/aesthetics-rest/merge", headers=owner, json=body
    )
    assert (waiting.status_code, waiting.json()["detail"]["error"]) == (
        409,
        "require_approval",
    )
    assert su(
        cluster,
        "SELECT count(*) FROM platform.authorization_decisions WHERE workspace_id = %s"
        " AND decision = 'require_approval'",
        (ws.id,),
    ) == [(1,)]
    approved = client.post(
        "/v1/catalog/concepts/domain/aesthetics-rest/merge",
        headers={**owner, "X-Empire-Approval": str(approval)},
        json=body,
    )
    assert approved.status_code == 200, approved.text


def test_non_admin_gets_403(cluster, client, auth, ws):  # AC-20
    with cluster.connect("platform_api") as conn:
        editor = add_member(conn, ws, "editor", "human")
        add_role(
            conn,
            ws,
            editor,
            "editor",
            ["catalog.concept:read", "catalog.concept:write"],
        )
        conn.commit()
    response = client.post(
        "/v1/catalog/concepts/domain/meaning/retire",
        headers=auth(editor, ws.id),
        json={"note": "x"},
    )
    assert response.status_code == 403


# --- MCP --------------------------------------------------------------------------------------------


def run_tool(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


def test_mcp_catalog_tools_are_read_only_and_work_with_mcp_scopes(
    client, auth, ws
):  # AC-52 (local)
    assert {"catalog.concept:read", "catalog.metric-definition:read"} <= set(MCP_SCOPES)
    assert not any(
        s.startswith("catalog.") and not s.endswith(":read") for s in MCP_SCOPES
    )
    token = auth(ws.owner, ws.id, list(MCP_SCOPES))["Authorization"].split()[1]
    server = build_server(
        client, None, lambda: token
    )  # the hosted adapter's shape (no graph client)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"catalog.concepts", "catalog.metrics", "catalog.review"} <= names
    assert not {n for n in names if n.startswith("catalog.")} - {
        "catalog.concepts",
        "catalog.metrics",
        "catalog.review",
    }
    concepts = run_tool(server, "catalog.concepts", {"kind": "value"})
    assert {c["slug"] for c in concepts["items"]} == {
        "freedom",
        "security",
        "vitality",
        "love",
        "growth",
        "beauty",
    }
    one = run_tool(server, "catalog.metrics", {"key": "time.work_minutes"})
    assert one["key"] == "time.work_minutes"
    assert run_tool(server, "catalog.review", {}) == {"items": []}
    with pytest.raises(
        ToolError
    ):  # protected definitions stay hidden: MCP_SCOPES has no diary read
        asyncio.run(server.call_tool("catalog.metrics", {"key": "diary.energy"}))
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "catalog.concepts", {"kind": "value", "actor": str(ws.owner)}
            )
        )


def test_owner_mcp_automation_carries_only_catalog_reads():  # AC-52 (deployed token scopes)
    from empire_platform.devdb import REPO_ROOT

    registry = json.loads(
        (
            REPO_ROOT / "contracts" / "automations" / "v1" / "automations.json"
        ).read_text()
    )
    scopes = next(a for a in registry["automations"] if a["id"] == "owner-mcp")[
        "scopes"
    ]
    catalog = {s for s in scopes if s.startswith("catalog.")}
    assert catalog == {"catalog.concept:read", "catalog.metric-definition:read"}


def test_contract_lists_every_catalog_route():  # AC-21 (test_tasks_contract keeps the file current)
    from empire_platform.contracts import OPENAPI

    paths = json.loads(OPENAPI.read_text(encoding="utf-8"))["paths"]
    expected = {
        "/v1/catalog/concepts": {"get", "post"},
        "/v1/catalog/concepts/{kind}/{slug}": {"patch"},
        "/v1/catalog/concepts/{kind}/{slug}/merge": {"post"},
        "/v1/catalog/concepts/{kind}/{slug}/split": {"post"},
        "/v1/catalog/concepts/{kind}/{slug}/retire": {"post"},
        "/v1/catalog/metrics": {"get"},
        "/v1/catalog/metrics/{key}": {"get", "put"},
        "/v1/catalog/review": {"get"},
        "/v1/catalog/versions": {"get"},
    }
    assert {p: set(paths[p]) for p in expected} == expected


def test_owner_permission_set_matches_the_migration(cluster):
    from empire_platform.permissions import CATALOG_OWNER

    assert set(CATALOG_OWNER) == set(CATALOG)
    sql = su(cluster, "SELECT prosrc FROM pg_proc WHERE proname = 'tenancy_signup'")[0][
        0
    ]
    assert all(p in sql for p in CATALOG_OWNER)
