"""Hosted empire-mcp: Clerk OAuth, the subject allowlist, and the narrow MCP issuer in tasks-api."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx
import jwt
import pytest
from empire_mcp.hosted import (
    CACHE_MAX_SECONDS,
    ClerkTokenVerifier,
    HostedConfig,
    HostedMCPServer,
    Subject,
    build_app,
    hosted_config,
    parse_subjects,
)
from empire_mcp.server import ToolFailed
from fastapi.testclient import TestClient
from graph_world import ISSUER, apps, bearer, gkeys, gtoken, gworld  # noqa: F401  (fixtures)
from tasks_api.app import create_app as create_tasks_app
from tasks_api.app import server_config
from tasks_api.auth import TokenVerifier, Unauthenticated

from empire_platform.devtoken import key_pair, mint
from empire_platform.token_exchange import MCP_SCOPES, McpTokenMinter

PUBLIC = "https://mcp.example.test"
CLERK = "https://clerk.example.test"
SECRET = "sk_test_never_logged"  # pragma: allowlist secret (synthetic test value)
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class FakeClerk:
    """Stands in for POST <Backend API>/oauth_applications/access_tokens/verify; other paths answer 404."""

    def __init__(
        self, path: str = "/v1/oauth_applications/access_tokens/verify"
    ) -> None:
        self.path = path
        self.tokens: dict[str, dict] = {}
        self.calls = 0
        self.paths: list[str] = []
        self.authorized: list[bool] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.paths.append(request.url.path)
        if request.url.path != self.path:
            return httpx.Response(
                404, json={"errors": [{"code": "resource_not_found"}]}
            )
        self.authorized.append(
            request.headers.get("authorization") == f"Bearer {SECRET}"
        )
        body = json.loads(request.content)
        found = self.tokens.get(body["access_token"])
        if found is None:
            return httpx.Response(400, json={"errors": [{"code": "token_invalid"}]})
        return httpx.Response(200, json=found)

    def issue(self, who: str | None, **overrides) -> str:
        token = f"oat_{uuid.uuid4().hex}"
        self.tokens[token] = {
            "object": "clerk_idp_oauth_access_token",
            "client_id": "client_1",
            "subject": who,
            "scopes": ["profile"],
            "revoked": False,
            "expired": False,
            "expiration": int(time.time()) + 3600,
            **overrides,
        }
        return token

    def verifier(self, **kwargs) -> ClerkTokenVerifier:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return ClerkTokenVerifier(SECRET, issuer=CLERK, client=client, **kwargs)


@pytest.fixture(scope="module")
def mcp_keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("mcp-keys"))


@pytest.fixture(scope="module")
def trusting_tasks(cluster, gkeys, mcp_keys):
    """A real Tasks API that trusts the main issuer and, narrowly, empire-mcp."""
    _, public_pem = gkeys
    verifier = TokenVerifier(
        public_pem,
        ISSUER,
        "tasks-api",
        mcp_issuer="empire-mcp",
        mcp_public_key_pem=mcp_keys[1],
    )
    return TestClient(create_tasks_app(lambda: cluster.connect("tasks_api"), verifier))


@pytest.fixture
def clerk():
    return FakeClerk()


def config(mcp_keys, subjects: dict[str, Subject]) -> HostedConfig:
    return HostedConfig(
        public_url=PUBLIC,
        clerk_issuer_url=CLERK,
        clerk_secret_key=SECRET,
        signing_key_pem=mcp_keys[0],
        subjects=subjects,
        tasks_api="http://tasks",
    )


@pytest.fixture
def hosted(mcp_keys, gworld, trusting_tasks, clerk):
    """Hosted app with owner and viewer mapped; records every bearer sent to the Tasks API."""
    sent: list[str] = []
    original = trusting_tasks.request

    def spy(method, url, **kwargs):
        sent.append(kwargs["headers"]["Authorization"].removeprefix("Bearer "))
        return original(method, url, **kwargs)

    trusting_tasks.request = spy  # type: ignore[method-assign]
    subjects = {
        "user_owner": Subject(gworld.owner, gworld.ws),
        "user_viewer": Subject(gworld.viewer, gworld.ws),
    }
    app = build_app(
        config(mcp_keys, subjects),
        tasks=trusting_tasks,
        graph=None,
        verifier=clerk.verifier(),
    )
    try:
        with TestClient(app, base_url=PUBLIC) as client:
            yield client, sent
    finally:
        trusting_tasks.request = original  # type: ignore[method-assign]


def rpc(client, token: str | None, method: str, params: dict | None = None, **headers):
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        **({"params": params} if params is not None else {}),
    }
    return client.post("/mcp", json=body, headers={**HEADERS, **auth, **headers})


def tool(client, token: str, name: str, arguments: dict) -> dict:
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


# --- HTTP surface ----------------------------------------------------------------------------------------------


def test_unauthenticated_mcp_request_gets_401_pointing_at_resource_metadata(hosted):
    client, sent = hosted
    response = rpc(client, None, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert rpc(client, "oat_unknown", "tools/list").status_code == 401
    assert sent == []


def test_protected_resource_metadata_names_the_clerk_issuer(hosted):
    client, _ = hosted
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == f"{PUBLIC}/mcp"
    assert metadata["authorization_servers"] == [CLERK]


def test_health_and_dns_rebinding_protection(hosted, clerk):
    client, _ = hosted
    assert client.get("/health").status_code == 200
    token = clerk.issue("user_owner")
    assert rpc(client, token, "tools/list").status_code == 200
    assert rpc(client, token, "tools/list", Host="attacker.example").status_code == 421
    assert (
        rpc(client, token, "tools/list", Origin="https://attacker.example").status_code
        == 403
    )


# --- allowlist -------------------------------------------------------------------------------------------------


def test_valid_clerk_token_for_unmapped_subject_is_denied_before_any_tool(
    hosted, clerk
):
    client, sent = hosted
    stranger = clerk.issue("user_stranger")
    for method, params in (
        ("tools/list", None),
        ("tools/call", {"name": "tasks.create", "arguments": {"title": "x"}}),
        (
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        ),
    ):
        response = rpc(client, stranger, method, params)
        assert response.status_code == 403, method
        assert response.json()["error"] == "insufficient_access"
        assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert sent == []  # no call reached the Tasks API


def test_tool_layer_refuses_unmapped_or_anonymous_callers_too(mcp_keys, trusting_tasks):
    server = HostedMCPServer("empire", subjects={})
    from empire_mcp.server import build_server

    build_server(
        trusting_tasks,
        None,
        None,
        mint=lambda audience: pytest.fail("minted for nobody"),
        server=server,
    )
    with pytest.raises(ToolFailed, match="403: subject_not_allowed"):
        asyncio.run(server.call_tool("tasks.list", {}))


def test_empty_or_missing_map_allows_nobody(mcp_keys, trusting_tasks, clerk):
    assert parse_subjects(None) == {}
    assert parse_subjects("") == {}
    assert parse_subjects("{}") == {}
    app = build_app(
        config(mcp_keys, {}),
        tasks=trusting_tasks,
        graph=None,
        verifier=clerk.verifier(),
    )
    with TestClient(app, base_url=PUBLIC) as client:
        assert rpc(client, clerk.issue("user_owner"), "tools/list").status_code == 403


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "not json",
        '{"user_a": {"identity": "x", "workspace": "y"}}',
        '{"user_a": {"identity": "%s"}}' % uuid.uuid4(),  # noqa: UP031 - JSON braces read clearer with %
        '{"user_a": {"identity": "%s", "workspace": "%s", "admin": true}}'  # noqa: UP031 - JSON braces read clearer with %
        % (uuid.uuid4(), uuid.uuid4()),
    ],
)
def test_malformed_subject_map_refuses_to_start(raw):
    with pytest.raises(SystemExit):
        parse_subjects(raw)


# --- through to a real Tasks API -------------------------------------------------------------------------------


def test_mapped_subject_lists_and_creates_with_attribution(
    cluster, hosted, clerk, gworld, mcp_keys, caplog
):
    caplog.set_level(logging.INFO, logger="tasks_api")
    client, sent = hosted
    token = clerk.issue("user_owner")
    names = {
        t["name"] for t in rpc(client, token, "tools/list").json()["result"]["tools"]
    }
    assert names == {
        "tasks.list",
        "tasks.get",
        "tasks.create",
        "tasks.update",
        "tasks.complete",
        "tasks.link",
    }

    created = tool(client, token, "tasks.create", {"title": "From Claude"})
    assert created["isError"] is False
    task = json.loads(created["content"][0]["text"])
    listed = json.loads(
        tool(client, token, "tasks.list", {"limit": 200})["content"][0]["text"]
    )
    assert task["id"] in {item["id"] for item in listed["items"]}

    with cluster.connect("postgres") as conn:
        row = conn.execute(
            "SELECT e.initiated_by, e.executed_by, e.on_behalf_of, e.delegation_id, e.channel, e.correlation_id"
            " FROM tasks.tasks t JOIN platform.executions e ON e.id = t.created_by_execution WHERE t.id = %s",
            (task["id"],),
        ).fetchone()
    assert row[:5] == (gworld.owner, gworld.owner, gworld.owner, None, "mcp")
    assert row[5].startswith("mcp-")
    writes = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "tasks_api"
    ]
    assert {
        "event": "tasks.write",
        "external_actor": "clerk:user_owner",
        "actor": str(gworld.owner),
    }.items() <= next(w for w in writes if w.get("event") == "tasks.write").items()

    claims = jwt.decode(
        sent[-1],
        mcp_keys[1],
        algorithms=["RS256"],
        audience="tasks-api",
        issuer="empire-mcp",
    )
    assert claims["sub"] == str(gworld.owner)
    assert claims["ws"] == str(gworld.ws)
    assert claims["act"] == {"sub": "clerk:user_owner"}
    assert claims["scope"].split() == list(MCP_SCOPES)
    assert "del" not in claims
    assert "obo" not in claims
    assert claims["exp"] - claims["iat"] <= 300
    assert len(
        {jwt.decode(t, options={"verify_signature": False})["jti"] for t in sent}
    ) == len(sent)


def test_viewer_cannot_exceed_real_permissions(hosted, clerk):
    client, _ = hosted
    viewer = clerk.issue("user_viewer")
    assert tool(client, viewer, "tasks.list", {})["isError"] is False
    denied = tool(client, viewer, "tasks.create", {"title": "Nope"})
    assert denied["isError"] is True
    assert "403" in denied["content"][0]["text"]


def test_mcp_token_cannot_delete_or_archive(trusting_tasks, mcp_keys, gworld, gtoken):
    task = trusting_tasks.post(
        "/v1/tasks",
        json={"title": "Keep me"},
        headers=bearer(gtoken(gworld.owner, gworld.ws)),
    ).json()
    token = McpTokenMinter(mcp_keys[0]).mint(
        "tasks-api", gworld.owner, gworld.ws, "clerk:user_owner"
    )
    headers = {**bearer(token), "X-Empire-Channel": "mcp"}
    for method, path, body in (
        ("DELETE", f"/v1/tasks/{task['id']}", None),
        ("POST", f"/v1/tasks/{task['id']}/archive", {"reason": "via mcp"}),
    ):
        response = trusting_tasks.request(method, path, headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == "scope_insufficient"


# --- tasks-api trusts the MCP issuer narrowly ------------------------------------------------------------------


def mcp_claims(gworld, ttl: int = 300, **overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "empire-mcp",
        "aud": "tasks-api",
        "sub": str(gworld.owner),
        "ws": str(gworld.ws),
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
        "scope": " ".join(MCP_SCOPES),
        "act": {"sub": "clerk:user_owner"},
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.mark.parametrize(
    ("overrides", "signer"),
    [
        ({"scope": " ".join([*MCP_SCOPES, "tasks.archive:write"])}, "mcp"),
        ({"scope": "tasks.task:delete"}, "mcp"),
        ({"scope": None}, "mcp"),
        ({"scope": ""}, "mcp"),
        ({"del": str(uuid.uuid4())}, "mcp"),
        ({"del": str(uuid.uuid4()), "obo": str(uuid.uuid4())}, "mcp"),
        ({"ttl": 301}, "mcp"),
        ({"ttl": 3600}, "mcp"),
        ({"act": None}, "mcp"),
        ({"iss": "empire-mcp-other"}, "mcp"),
        ({}, "main"),  # right issuer, wrong key
        ({}, "stranger"),
        ({"aud": "graph-api"}, "mcp"),
    ],
)
def test_tasks_api_rejects_bad_mcp_tokens(
    trusting_tasks, gkeys, mcp_keys, gworld, tmp_path, overrides, signer
):
    key = {"mcp": mcp_keys[0], "main": gkeys[0], "stranger": key_pair(tmp_path)[0]}[
        signer
    ]
    token = jwt.encode(mcp_claims(gworld, **overrides), key, algorithm="RS256")
    response = trusting_tasks.get(
        "/v1/tasks", headers={**bearer(token), "X-Empire-Channel": "mcp"}
    )
    assert response.status_code == 401, (overrides, signer)


def test_valid_mcp_token_is_read_only_on_its_scopes_and_channel(
    trusting_tasks, mcp_keys, gworld
):
    token = jwt.encode(
        mcp_claims(gworld, scope="tasks.task:read"), mcp_keys[0], algorithm="RS256"
    )
    assert (
        trusting_tasks.get(
            "/v1/tasks", headers={**bearer(token), "X-Empire-Channel": "mcp"}
        ).status_code
        == 200
    )
    assert (
        trusting_tasks.get("/v1/tasks", headers=bearer(token)).status_code == 200
    )  # channel defaults to mcp
    assert (
        trusting_tasks.get(
            "/v1/tasks", headers={**bearer(token), "X-Empire-Channel": "api"}
        ).status_code
        == 401
    )
    denied = trusting_tasks.post(
        "/v1/tasks", json={"title": "x"}, headers=bearer(token)
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "scope_insufficient"


def test_mcp_tokens_are_refused_where_the_issuer_is_not_trusted(apps, mcp_keys, gworld):
    tasks, _ = apps  # the ordinary app: no MCP issuer configured
    token = McpTokenMinter(mcp_keys[0]).mint(
        "tasks-api", gworld.owner, gworld.ws, "clerk:user_owner"
    )
    assert tasks.get("/v1/tasks", headers=bearer(token)).status_code == 401


def test_main_issuer_tokens_are_unaffected(
    trusting_tasks, gkeys, mcp_keys, gworld, gtoken
):
    owner = gtoken(gworld.owner, gworld.ws)
    assert trusting_tasks.get("/v1/tasks", headers=bearer(owner)).status_code == 200
    delegated = gtoken(
        gworld.coach,
        gworld.ws,
        delegation=gworld.coach_rw,
        obo=gworld.owner,
        act=gworld.owner,
    )
    principal = TokenVerifier(
        gkeys[1], ISSUER, mcp_issuer="empire-mcp", mcp_public_key_pem=mcp_keys[1]
    ).verify(f"Bearer {delegated}", "api")
    assert (principal.delegation, principal.initiated_by, principal.external_actor) == (
        gworld.coach_rw,
        gworld.owner,
        None,
    )
    # Main-issuer tokens signed with the MCP key are not accepted, and the 1 hour cap still holds.
    forged = mint(
        mcp_keys[0], sub=gworld.owner, ws=gworld.ws, issuer=ISSUER, audience="tasks-api"
    )
    assert trusting_tasks.get("/v1/tasks", headers=bearer(forged)).status_code == 401
    long = gtoken(gworld.owner, gworld.ws, ttl=3601)
    assert trusting_tasks.get("/v1/tasks", headers=bearer(long)).status_code == 401


def test_tasks_api_mcp_subject_pairs_gate_mcp_tokens(
    cluster, gkeys, mcp_keys, gworld, gtoken
):
    pinned = TestClient(
        create_tasks_app(
            lambda: cluster.connect("tasks_api"),
            TokenVerifier(
                gkeys[1],
                ISSUER,
                "tasks-api",
                mcp_issuer="empire-mcp",
                mcp_public_key_pem=mcp_keys[1],
                mcp_subjects=frozenset({(gworld.owner, gworld.ws)}),
            ),
        )
    )
    minter = McpTokenMinter(mcp_keys[0])
    owner = minter.mint("tasks-api", gworld.owner, gworld.ws, "clerk:user_owner")
    viewer = minter.mint("tasks-api", gworld.viewer, gworld.ws, "clerk:user_viewer")
    other_ws = minter.mint("tasks-api", gworld.owner, gworld.beta, "clerk:user_owner")
    assert pinned.get("/v1/tasks", headers=bearer(owner)).status_code == 200
    assert pinned.get("/v1/tasks", headers=bearer(viewer)).status_code == 401
    assert pinned.get("/v1/tasks", headers=bearer(other_ws)).status_code == 401
    # Main-issuer tokens are not subject to the MCP pairs.
    assert (
        pinned.get(
            "/v1/tasks", headers=bearer(gtoken(gworld.viewer, gworld.ws))
        ).status_code
        == 200
    )


def test_server_config_reads_mcp_subject_pairs(gkeys, mcp_keys):
    base = {
        "EMPIRE_DB_MODE": "hosted",
        "TASKS_API_JWT_PUBLIC_KEY_PEM": gkeys[1],
        "TASKS_API_JWT_ISSUER": "empire-prod",
        "TASKS_API_MCP_PUBLIC_KEY_PEM": mcp_keys[1],
    }
    sub, ws = uuid.uuid4(), uuid.uuid4()
    assert server_config(base).mcp_subjects is None
    assert server_config(
        {
            **base,
            "TASKS_API_MCP_SUBJECTS": json.dumps([{"sub": str(sub), "ws": str(ws)}]),
        }
    ).mcp_subjects == frozenset({(sub, ws)})
    assert (
        server_config({**base, "TASKS_API_MCP_SUBJECTS": "[]"}).mcp_subjects
        == frozenset()
    )
    for bad in (
        "{}",
        "nope",
        '[{"sub": "x", "ws": "y"}]',
        json.dumps([{"sub": str(sub)}]),
    ):
        with pytest.raises(SystemExit, match="TASKS_API_MCP_SUBJECTS"):
            server_config({**base, "TASKS_API_MCP_SUBJECTS": bad})
    # Kill switch: without the MCP key the pairs are ignored, even when malformed.
    no_key = {k: v for k, v in base.items() if k != "TASKS_API_MCP_PUBLIC_KEY_PEM"}
    assert (
        server_config({**no_key, "TASKS_API_MCP_SUBJECTS": "nope"}).mcp_issuer is None
    )


def test_verifier_refuses_ambiguous_mcp_configuration(gkeys, mcp_keys):
    with pytest.raises(ValueError, match="must differ from the main issuer"):
        TokenVerifier(
            gkeys[1],
            "empire-mcp",
            mcp_issuer="empire-mcp",
            mcp_public_key_pem=mcp_keys[1],
        )
    with pytest.raises(ValueError, match="must be given together"):
        TokenVerifier(gkeys[1], ISSUER, mcp_issuer="empire-mcp")
    with pytest.raises(Unauthenticated):
        TokenVerifier(gkeys[1], ISSUER).verify(None, None)


def test_server_config_trusts_the_mcp_issuer_only_with_its_own_key(gkeys, mcp_keys):
    base = {
        "EMPIRE_DB_MODE": "hosted",
        "TASKS_API_JWT_PUBLIC_KEY_PEM": gkeys[1],
        "TASKS_API_JWT_ISSUER": "empire-prod",
    }
    plain = server_config(base)
    assert (plain.mcp_issuer, plain.mcp_public_key_pem) == (None, None)
    trusted = server_config({**base, "TASKS_API_MCP_PUBLIC_KEY_PEM": mcp_keys[1]})
    assert (trusted.mcp_issuer, trusted.mcp_public_key_pem) == (
        "empire-mcp",
        mcp_keys[1].strip(),
    )
    assert (
        server_config(
            {
                **base,
                "TASKS_API_MCP_PUBLIC_KEY_PEM": mcp_keys[1],
                "TASKS_API_TRUSTED_MCP_ISSUER": "mcp-prod",
            }
        ).mcp_issuer
        == "mcp-prod"
    )
    for bad in (
        {"TASKS_API_TRUSTED_MCP_ISSUER": "empire-prod"},
        {"TASKS_API_TRUSTED_MCP_ISSUER": "empire-platform-dev"},
        {"TASKS_API_MCP_PUBLIC_KEY_PEM": gkeys[1]},
    ):
        with pytest.raises(SystemExit):
            server_config({**base, "TASKS_API_MCP_PUBLIC_KEY_PEM": mcp_keys[1], **bad})
    dev_public = key_pair()[1]
    with pytest.raises(SystemExit, match="dev public key"):
        server_config({**base, "TASKS_API_MCP_PUBLIC_KEY_PEM": dev_public})
    # The existing hosted refusals still hold.
    with pytest.raises(SystemExit):
        server_config(
            {"EMPIRE_DB_MODE": "hosted", "TASKS_API_MCP_PUBLIC_KEY_PEM": mcp_keys[1]}
        )


def test_minter_caps_lifetime_and_audiences(mcp_keys, gworld):
    with pytest.raises(ValueError, match="ttl must be between"):
        McpTokenMinter(mcp_keys[0], ttl=301)
    from empire_platform.token_exchange import ExchangeRefused

    with pytest.raises(ExchangeRefused):
        McpTokenMinter(mcp_keys[0]).mint(
            "finance-api", gworld.owner, gworld.ws, "clerk:x"
        )


# --- Clerk verifier --------------------------------------------------------------------------------------------


def test_clerk_verifier_accepts_live_tokens_and_sends_the_secret(clerk):
    verifier = clerk.verifier()
    token = clerk.issue("user_owner")
    access = asyncio.run(verifier.verify_token(token))
    assert access is not None
    assert access.subject == "user_owner"
    assert access.client_id == "client_1"
    assert clerk.authorized == [True]
    assert SECRET not in repr(verifier)


@pytest.mark.parametrize(
    "overrides",
    [
        {"revoked": True},
        {"expired": True},
        {"expiration": int(time.time()) - 1},
        {"expiration": int(time.time() * 1000) - 1000},
        {"subject": None},
        {"subject": ""},
    ],
)
def test_clerk_verifier_rejects_revoked_expired_or_subjectless_tokens(clerk, overrides):
    token = clerk.issue("user_owner", **overrides)
    assert asyncio.run(clerk.verifier().verify_token(token)) is None


def test_clerk_verifier_rejects_non_200_bad_bodies_and_outages():
    def outage(request):
        raise httpx.ConnectTimeout("timed out")

    for handler in (
        lambda r: httpx.Response(401, json={}),
        lambda r: httpx.Response(500, text="x"),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json=[1]),
        outage,
    ):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert (
            asyncio.run(ClerkTokenVerifier(SECRET, client=client).verify_token("oat_x"))
            is None
        )


def test_clerk_cache_never_outlives_60_seconds(clerk):
    now = [1000.0]
    verifier = clerk.verifier(clock=lambda: now[0])
    token = clerk.issue("user_owner", expiration=None)
    assert asyncio.run(verifier.verify_token(token)) is not None
    assert all(
        len(key) == 64 and token not in key for key in verifier._cache
    )  # SHA-256 keys only
    now[0] += CACHE_MAX_SECONDS - 1
    assert asyncio.run(verifier.verify_token(token)) is not None
    assert clerk.calls == 1
    clerk.tokens[token]["revoked"] = (
        True  # revoked at Clerk: visible as soon as the cache entry ends
    )
    now[0] += 1
    assert asyncio.run(verifier.verify_token(token)) is None
    assert clerk.calls == 2
    with pytest.raises(ValueError, match="cache_seconds must be between"):
        clerk.verifier(cache_seconds=CACHE_MAX_SECONDS + 1)


def test_clerk_cache_ends_with_the_token(clerk):
    now = [time.time()]
    verifier = clerk.verifier(clock=lambda: now[0])
    token = clerk.issue("user_owner", expiration=int(now[0]) + 10)
    assert asyncio.run(verifier.verify_token(token)) is not None
    now[0] += 11
    assert asyncio.run(verifier.verify_token(token)) is None
    assert clerk.calls == 2


def test_failures_are_cached_for_15_seconds_only(clerk):
    now = [1000.0]
    verifier = clerk.verifier(clock=lambda: now[0])
    assert asyncio.run(verifier.verify_token("oat_later")) is None
    clerk.tokens["oat_later"] = {
        "subject": "user_owner",
        "revoked": False,
        "expired": False,
    }
    now[0] += 14
    assert asyncio.run(verifier.verify_token("oat_later")) is None
    assert clerk.calls == 1
    assert all(len(key) == 64 for key in verifier._negative)
    now[0] += 1
    assert asyncio.run(verifier.verify_token("oat_later")) is not None
    assert clerk.calls == 2


def test_negative_cache_is_bounded(clerk, monkeypatch):
    import empire_mcp.hosted as hosted_module

    monkeypatch.setattr(hosted_module, "CACHE_MAX_ENTRIES", 5)
    verifier = clerk.verifier()
    for n in range(12):
        asyncio.run(verifier.verify_token(f"oat_bad_{n}"))
    assert len(verifier._negative) <= 5


@pytest.mark.parametrize(
    "token",
    ["", "a" * 4097, "oat x", "oat\tx", "oat\nx", "oat\x00x", "oat\x7fx", "oat\u00a0x"],
)
def test_malformed_bearer_tokens_never_reach_clerk(clerk, token):
    assert asyncio.run(clerk.verifier().verify_token(token)) is None
    assert clerk.calls == 0


def test_verify_path_defaults_to_v1_and_falls_back_once_past_a_404():
    v1 = FakeClerk()
    token = v1.issue("user_owner")
    verifier = v1.verifier()
    assert asyncio.run(verifier.verify_token(token)) is not None
    assert v1.paths == ["/v1/oauth_applications/access_tokens/verify"]

    bare = FakeClerk(path="/oauth_applications/access_tokens/verify")
    token, other = bare.issue("user_owner"), bare.issue("user_owner")
    verifier = bare.verifier()
    assert asyncio.run(verifier.verify_token(token)) is not None
    assert bare.paths == [
        "/v1/oauth_applications/access_tokens/verify",
        "/oauth_applications/access_tokens/verify",
    ]
    assert (
        asyncio.run(verifier.verify_token(other)) is not None
    )  # the working path is remembered
    assert bare.paths[2:] == ["/oauth_applications/access_tokens/verify"]

    nowhere = FakeClerk(path="/elsewhere")
    assert (
        asyncio.run(nowhere.verifier().verify_token(nowhere.issue("user_owner")))
        is None
    )  # 404 is never success
    assert len(nowhere.paths) == 2


def test_overridden_api_url_is_used_as_given():
    clerk = FakeClerk(path="/custom/oauth_applications/access_tokens/verify")
    verifier = clerk.verifier(api_url="https://clerk.internal.test/custom")
    assert asyncio.run(verifier.verify_token(clerk.issue("user_owner"))) is not None
    assert clerk.paths == ["/custom/oauth_applications/access_tokens/verify"]


def test_check_clerk_reports_paths_without_the_key(capsys, monkeypatch):
    from empire_mcp import hosted as hosted_module

    clerk = FakeClerk()
    code, lines = hosted_module.check_clerk(
        SECRET, client=httpx.Client(transport=httpx.MockTransport(clerk.handler))
    )
    assert code == 0
    assert lines[0].endswith("path ok (400 for a bogus token, codes: token_invalid)")
    assert "path not found (404" in lines[1]
    assert not any(SECRET in line for line in lines)
    nowhere = FakeClerk(path="/elsewhere")
    code, lines = hosted_module.check_clerk(
        SECRET, client=httpx.Client(transport=httpx.MockTransport(nowhere.handler))
    )
    assert code == 1
    assert all("path not found" in line for line in lines)

    monkeypatch.setattr(
        hosted_module,
        "check_clerk",
        lambda key, url: (0, [f"{url}: path ok (401 for a bogus token)"]),
    )
    monkeypatch.setenv("CLERK_SECRET_KEY", SECRET)
    monkeypatch.delenv("CLERK_API_URL", raising=False)
    assert hosted_module.main(["--check-clerk"]) == 0
    out = capsys.readouterr()
    assert "https://api.clerk.com/v1" in out.out
    assert SECRET not in out.out + out.err
    monkeypatch.delenv("CLERK_SECRET_KEY")
    assert hosted_module.main(["--check-clerk"]) == 2


# --- client pinning and token shape ---------------------------------------------------------------------------


def test_pinned_clients_reject_other_and_missing_client_ids(clerk):
    verifier = clerk.verifier(allowed_clients=frozenset({"client_1"}))
    assert asyncio.run(verifier.verify_token(clerk.issue("user_owner"))) is not None
    assert (
        asyncio.run(
            verifier.verify_token(clerk.issue("user_owner", client_id="client_evil"))
        )
        is None
    )
    assert (
        asyncio.run(verifier.verify_token(clerk.issue("user_owner", client_id=None)))
        is None
    )
    missing = clerk.issue("user_owner")
    del clerk.tokens[missing]["client_id"]
    assert asyncio.run(verifier.verify_token(missing)) is None
    # Without pinning, a missing client id is tolerated but never mistaken for a pinned one.
    unpinned = clerk.verifier()
    access = asyncio.run(
        unpinned.verify_token(clerk.issue("user_owner", client_id=None))
    )
    assert access is not None
    assert access.client_id == "unknown"


def test_unpinned_client_is_refused_before_the_allowlist(
    mcp_keys, trusting_tasks, gworld, clerk
):
    subjects = {"user_owner": Subject(gworld.owner, gworld.ws)}
    app = build_app(
        config(mcp_keys, subjects),
        tasks=trusting_tasks,
        graph=None,
        verifier=clerk.verifier(allowed_clients=frozenset({"client_1"})),
    )
    with TestClient(app, base_url=PUBLIC) as client:
        assert (
            rpc(
                client,
                clerk.issue("user_owner", client_id="client_other"),
                "tools/list",
            ).status_code
            == 401
        )
        assert rpc(client, clerk.issue("user_owner"), "tools/list").status_code == 200


@pytest.mark.parametrize(
    ("kind", "accepted"),
    [
        ("clerk_idp_oauth_access_token", True),
        ("oauth_access_token", True),
        ("session_token", False),
        ("m2m_token", False),
        ("api_key", False),
        (None, False),
        ("", False),
    ],
)
def test_only_oauth_access_token_objects_are_accepted(clerk, kind, accepted):
    token = clerk.issue("user_owner", object=kind)
    assert (asyncio.run(clerk.verifier().verify_token(token)) is not None) is accepted
    absent = clerk.issue("user_owner")
    del clerk.tokens[absent]["object"]
    assert asyncio.run(clerk.verifier().verify_token(absent)) is not None


# --- rate limit ------------------------------------------------------------------------------------------------


def test_failed_attempts_are_rate_limited_per_ip(
    mcp_keys, trusting_tasks, gworld, clerk
):
    from empire_mcp.hosted import FailedAttemptLimiter

    now = [0.0]
    limiter = FailedAttemptLimiter(limit=3, clock=lambda: now[0])
    subjects = {"user_owner": Subject(gworld.owner, gworld.ws)}
    cfg = HostedConfig(
        **{**config(mcp_keys, subjects).__dict__, "trust_forwarded": True}
    )
    app = build_app(
        cfg,
        tasks=trusting_tasks,
        graph=None,
        verifier=clerk.verifier(),
        limiter=limiter,
    )
    good = clerk.issue("user_owner")
    with TestClient(app, base_url=PUBLIC) as client:
        attacker = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        for _ in range(3):
            assert rpc(client, None, "tools/list", **attacker).status_code == 401
        calls = clerk.calls
        assert rpc(client, good, "tools/list", **attacker).status_code == 429
        assert clerk.calls == calls  # blocked before Clerk
        assert client.get("/health", headers=attacker).status_code == 200
        assert (
            rpc(
                client, good, "tools/list", **{"X-Forwarded-For": "198.51.100.7"}
            ).status_code
            == 200
        )
        now[0] += 61
        assert rpc(client, good, "tools/list", **attacker).status_code == 200


def test_forwarded_for_is_ignored_unless_the_platform_sets_it():
    from empire_mcp.hosted import client_ip

    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")],
        "client": ("10.1.1.1", 5000),
    }
    assert client_ip(scope, True) == "203.0.113.9"
    assert client_ip(scope, False) == "10.1.1.1"


# --- event loop ------------------------------------------------------------------------------------------------


def test_cell_calls_run_off_the_event_loop(hosted, clerk, trusting_tasks):
    client, _ = hosted
    seen: list[bool] = []
    spy = trusting_tasks.request

    def probe(method, url, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append(False)
        except RuntimeError:
            seen.append(True)  # worker thread: no running loop to block
        return spy(method, url, **kwargs)

    trusting_tasks.request = probe  # type: ignore[method-assign]
    try:
        assert (
            tool(client, clerk.issue("user_owner"), "tasks.list", {})["isError"]
            is False
        )
    finally:
        trusting_tasks.request = spy  # type: ignore[method-assign]
    assert seen == [True]


# --- repr hygiene ----------------------------------------------------------------------------------------------


def test_signing_keys_never_appear_in_repr(mcp_keys, gkeys):
    from empire_platform.token_exchange import TokenExchanger

    assert "PRIVATE" not in repr(McpTokenMinter(mcp_keys[0]))
    assert "PRIVATE" not in repr(TokenExchanger(gkeys[0], gkeys[1], ISSUER))


# --- configuration and logs ------------------------------------------------------------------------------------


def test_hosted_config_requires_its_settings(mcp_keys, gworld):
    env = {
        "EMPIRE_MCP_PUBLIC_URL": PUBLIC,
        "CLERK_ISSUER_URL": CLERK,
        "CLERK_SECRET_KEY": SECRET,
        "EMPIRE_MCP_SIGNING_KEY_PEM": mcp_keys[0],
        "EMPIRE_TASKS_API": "http://tasks-api.railway.internal:8080",
        "EMPIRE_MCP_SUBJECTS": json.dumps(
            {"user_owner": {"identity": str(gworld.owner), "workspace": str(gworld.ws)}}
        ),
        "PORT": "9100",
    }
    loaded = hosted_config(env)
    assert loaded.resource_url == f"{PUBLIC}/mcp"
    assert loaded.port == 9100
    assert loaded.graph_api is None
    assert loaded.subjects == {"user_owner": Subject(gworld.owner, gworld.ws)}
    assert SECRET not in repr(loaded)
    assert "PRIVATE" not in repr(loaded)
    for name in (
        "EMPIRE_MCP_PUBLIC_URL",
        "CLERK_ISSUER_URL",
        "CLERK_SECRET_KEY",
        "EMPIRE_MCP_SIGNING_KEY_PEM",
        "EMPIRE_TASKS_API",
    ):
        with pytest.raises(SystemExit, match=name):
            hosted_config({k: v for k, v in env.items() if k != name})
    for url in ("http://mcp.example.test", "https://mcp.example.test/mcp", "ftp://x"):
        with pytest.raises(SystemExit):
            hosted_config({**env, "EMPIRE_MCP_PUBLIC_URL": url})
    assert (
        hosted_config(
            {k: v for k, v in env.items() if k != "EMPIRE_MCP_SUBJECTS"}
        ).subjects
        == {}
    )
    for url in (
        "http://tasks-api.example.com",
        "http://tasks-api.railway.internal.evil.com",
        "ftp://x",
        "https://tasks.example.com?x=1",
    ):
        with pytest.raises(SystemExit, match="EMPIRE_TASKS_API"):
            hosted_config({**env, "EMPIRE_TASKS_API": url})
    with pytest.raises(SystemExit, match="EMPIRE_GRAPH_API"):
        hosted_config({**env, "EMPIRE_GRAPH_API": "http://graph.example.com"})
    for url in (
        "https://tasks.example.com",
        "http://127.0.0.1:3601",
        "http://localhost:3601",
    ):
        assert hosted_config({**env, "EMPIRE_TASKS_API": url}).tasks_api == url
    assert loaded.clerk_api_url == "https://api.clerk.com/v1"
    assert loaded.trust_forwarded is False
    assert hosted_config({**env, "RAILWAY_ENVIRONMENT_ID": "x"}).trust_forwarded is True
    assert (
        hosted_config({**env, "CLERK_API_URL": "https://api.clerk.com"}).clerk_api_url
        == "https://api.clerk.com"
    )


def test_client_pinning_config_and_startup_warning(mcp_keys, caplog):
    env = {
        "EMPIRE_MCP_PUBLIC_URL": PUBLIC,
        "CLERK_ISSUER_URL": CLERK,
        "CLERK_SECRET_KEY": SECRET,
        "EMPIRE_MCP_SIGNING_KEY_PEM": mcp_keys[0],
        "EMPIRE_TASKS_API": "https://tasks.example.com",
    }
    caplog.set_level(logging.INFO, logger="empire_mcp.hosted")
    assert hosted_config(env).allowed_clients is None
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "client pinning is off" in r.getMessage()
    ]
    assert len(warnings) == 1
    caplog.clear()
    pinned = hosted_config(
        {**env, "EMPIRE_MCP_ALLOWED_CLIENTS": " client_a, client_b ,,"}
    )
    assert pinned.allowed_clients == frozenset({"client_a", "client_b"})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert SECRET not in caplog.text
    assert "PRIVATE" not in caplog.text


def test_graph_tools_only_when_a_graph_api_is_configured(
    mcp_keys, trusting_tasks, clerk, gworld
):
    subjects = {"user_owner": Subject(gworld.owner, gworld.ws)}
    graph = httpx.Client(base_url="http://graph")
    app = build_app(
        config(mcp_keys, subjects),
        tasks=trusting_tasks,
        graph=graph,
        verifier=clerk.verifier(),
    )
    with TestClient(app, base_url=PUBLIC) as client:
        names = {
            t["name"]
            for t in rpc(client, clerk.issue("user_owner"), "tools/list").json()[
                "result"
            ]["tools"]
        }
    assert {"graph.search", "graph.neighbors"} <= names


def test_tokens_never_appear_in_logs(hosted, clerk, caplog):
    caplog.set_level(logging.DEBUG)
    client, sent = hosted
    good, stranger, revoked = (
        clerk.issue("user_owner"),
        clerk.issue("user_stranger"),
        clerk.issue("user_owner", revoked=True),
    )
    tool(client, good, "tasks.create", {"title": "Logged safely"})
    rpc(client, stranger, "tools/list")
    rpc(client, revoked, "tools/list")
    rpc(client, "oat_unknown_token_value", "tools/list")
    text = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert sent
    for secret in (good, stranger, revoked, "oat_unknown_token_value", SECRET, *sent):
        assert secret not in text
    assert "subject_not_allowed" in text
    assert "revoked" in text
