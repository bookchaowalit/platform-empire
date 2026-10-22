"""platform-identity step 3: the identity_api role, the shadow mirror, and decision parity.

The parity tests run the real platform-identity service against this
session's database and compare it with the platform endpoint request by
request. They are skipped when the ``platform_identity`` package is not
installed (``pip install -e ../../../bookchaowalit-backend/public/book-backend/platform-identity``).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt
import psycopg
import pytest
from conftest import add_member, create_workspace, delegate
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_api.identity_shadow import IdentityShadow, shadow_url
from platform_api.routes import register
from tasks_api.auth import TokenVerifier

from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import DECISIONS_OWNER

ISSUER = "empire-platform-test"
GOOGLE_CLIENT = "client.apps.googleusercontent.com"


@dataclass
class World:
    ws: uuid.UUID
    other_ws: uuid.UUID
    owner: uuid.UUID
    partner: uuid.UUID
    reader: uuid.UUID
    read_delegation: uuid.UUID


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("shadow-keys"))


@pytest.fixture(scope="module")
def world(cluster) -> World:
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        ws = create_workspace(conn, f"shadow-{suffix}", list(DECISIONS_OWNER))
        partner = add_member(conn, ws, "partner", "human")
        reader = add_member(conn, ws, "decision-reader", "agent")
        read_delegation = delegate(
            conn, ws, ws.owner, reader, ["decisions.decision:read"]
        )
        other = create_workspace(conn, f"shadow-other-{suffix}", list(DECISIONS_OWNER))
        conn.commit()
    return World(ws.id, other.id, ws.owner, partner, reader, read_delegation)


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall() if cursor.description else None
        conn.commit()
        return rows


def decisions_recorded(cluster) -> int:
    return superuser(cluster, "SELECT count(*) FROM platform.authorization_decisions")[
        0
    ][0]


# --- the identity_api role ----------------------------------------------------


def test_identity_api_role_has_no_elevated_attributes(cluster):
    row = superuser(
        cluster,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit"
        " FROM pg_roles WHERE rolname = 'identity_api'",
    )
    assert row == [(False, False, False, False, False)]


def test_identity_api_may_decide_and_read_memberships_only(cluster, world):
    with cluster.connect("identity_api") as conn:
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(world.ws),)
        )
        decision = conn.execute(
            "SELECT decision FROM platform.authorize(%s, %s, 'decisions.decision:read', 'decision', 'd-1', %s)",
            (world.owner, world.ws, world.ws),
        ).fetchone()[0]
        assert decision == "allow"
        members = conn.execute("SELECT count(*) FROM platform.memberships").fetchone()[
            0
        ]
        assert (
            members == 3
        )  # owner, partner, reader: RLS still limits it to the bound workspace
        conn.rollback()
    forbidden = [
        "SELECT display_name FROM platform.identities",
        "SELECT * FROM platform.authorization_decisions",
        "SELECT * FROM platform.delegations",
        "SELECT * FROM platform.role_permissions",
        "UPDATE platform.memberships SET status = 'suspended'",
        "INSERT INTO platform.identities (kind, display_name) VALUES ('human', 'x')",
    ]
    for statement in forbidden:
        with (
            cluster.connect("identity_api") as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(statement)


# --- the mirror ----------------------------------------------------------------


def mirror(handler) -> IdentityShadow:
    return IdentityShadow(
        "https://identity.test/v1/authz/decide",
        httpx.Client(transport=httpx.MockTransport(handler)),
        start=False,
    )


def test_mirror_logs_a_match_without_token_or_resource(caplog):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["channel"] = request.headers.get("x-empire-channel")
        return httpx.Response(
            200, json={"decision": "allow", "reason": "allowed", "decision_id": None}
        )

    with caplog.at_level(logging.INFO, logger="platform_api.identity_shadow"):
        mirror(handler)(
            {"action": "a.b:read", "resource_type": "t", "resource_id": "secret-id"},
            "Bearer tok",
            "mcp",
            "allow",
            "allowed",
        )
    assert seen == {"auth": "Bearer tok", "channel": "mcp"}
    assert "outcome=match action=a.b:read" in caplog.text
    assert "tok" not in caplog.text
    assert "secret-id" not in caplog.text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                200, json={"decision": "deny", "reason": "no_permission\nforged"}
            ),
            "outcome=mismatch",
        ),
        (httpx.Response(401, json={}), "shadow_status=401"),
        (httpx.Response(200, text="not json"), "outcome=invalid_response"),
    ],
)
def test_mirror_logs_differences(caplog, response, expected):
    with caplog.at_level(logging.INFO, logger="platform_api.identity_shadow"):
        mirror(lambda _: response)(
            {"action": "a.b:read"}, "Bearer t", None, "allow", "allowed"
        )
    assert expected in caplog.text
    assert "\nforged" not in caplog.text


def test_mirror_failure_never_raises(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down")

    with caplog.at_level(logging.INFO, logger="platform_api.identity_shadow"):
        mirror(handler)({"action": "a.b:read"}, "Bearer t", None, "allow", "allowed")
    assert "outcome=unreachable" in caplog.text


def test_shadow_url_requires_https():
    assert shadow_url(None) is None
    assert shadow_url(" ") is None
    assert (
        shadow_url("https://identity.example/")
        == "https://identity.example/v1/authz/decide"
    )
    for bad in ("http://identity.example", "http://localhost.evil.example", "https://"):
        with pytest.raises(SystemExit):
            shadow_url(bad)


def test_queued_mirror_drops_instead_of_blocking(caplog):
    import threading

    release = threading.Event()
    answered = []

    def handler(request: httpx.Request) -> httpx.Response:
        release.wait(5)
        answered.append(1)
        return httpx.Response(200, json={"decision": "allow", "reason": "allowed"})

    shadow = IdentityShadow(
        "https://identity.test/v1/authz/decide",
        httpx.Client(transport=httpx.MockTransport(handler)),
        queue_size=2,
    )
    with caplog.at_level(logging.INFO, logger="platform_api.identity_shadow"):
        for _ in range(10):  # returns at once even though the worker is stuck
            shadow({"action": "a.b:read"}, "Bearer t", None, "allow", "allowed")
        assert shadow.dropped >= 7
        release.set()
        shadow._queue.join()
    assert 1 <= len(answered) <= 3
    assert "outcome=dropped" in caplog.text


def test_decide_hands_answered_decisions_to_the_mirror(cluster, keys, world):
    private_pem, public_pem = keys
    calls = []
    app = FastAPI()
    register(
        app,
        connect_platform=lambda: cluster.connect("tasks_api"),
        platform_verifier=TokenVerifier(public_pem, ISSUER, audience="platform-api"),
        graph_verifier=TokenVerifier(public_pem, ISSUER, audience="graph-api"),
        identity_shadow=lambda *args: calls.append(args),
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {mint(private_pem, sub=world.owner, ws=world.ws, issuer=ISSUER)}"
    }
    body = {
        "action": "decisions.decision:read",
        "resource_type": "decision",
        "resource_id": "d-1",
    }
    assert (
        client.post("/v1/authz/decide", headers=headers, json=body).status_code == 200
    )
    assert calls == [(body, headers["Authorization"], None, "allow", "allowed")]
    assert client.post("/v1/authz/decide", json=body).status_code == 401
    assert len(calls) == 1, "refused requests are not mirrored"


# --- parity with the real identity service --------------------------------------


@pytest.fixture(scope="module")
def google_keys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("google-keys"))


@pytest.fixture(scope="module")
def clients(cluster, keys, google_keys, tmp_path_factory):
    platform_identity = pytest.importorskip(
        "platform_identity.app", reason="platform-identity is not installed"
    )
    from platform_identity.auth import TokenVerifier as IdentityVerifier
    from platform_identity.google import GoogleVerifier
    from platform_identity.keys import SigningKey, StaticKey

    _, public_pem = keys
    signing_private, _ = key_pair(tmp_path_factory.mktemp("identity-signing"))
    primary = FastAPI()
    register(
        primary,
        connect_platform=lambda: cluster.connect("tasks_api"),
        platform_verifier=TokenVerifier(public_pem, ISSUER, audience="platform-api"),
        graph_verifier=TokenVerifier(public_pem, ISSUER, audience="graph-api"),
    )
    identity = platform_identity.create_app(
        connect=lambda: cluster.connect("identity_api"),
        platform_verifier=IdentityVerifier(StaticKey(public_pem), ISSUER),
        signing_key=SigningKey(signing_private),
        issuer="https://identity.test",
        google=GoogleVerifier(StaticKey(google_keys[1]), frozenset({GOOGLE_CLIENT})),
        token_audiences=frozenset({"tasks-api"}),
    )
    return TestClient(primary), TestClient(identity)


def test_shadow_decisions_match_the_platform_and_record_nothing(
    clients, keys, world, cluster
):
    primary, identity = clients
    private_pem, _ = keys

    def token(sub, ws, **kwargs):
        return {
            "Authorization": f"Bearer {mint(private_pem, sub=sub, ws=ws, issuer=ISSUER, **kwargs)}"
        }

    read = {
        "action": "decisions.decision:read",
        "resource_type": "decision",
        "resource_id": "d-1",
    }
    write = {**read, "action": "decisions.decision:write"}
    cases = [
        (token(world.owner, world.ws), read),
        (token(world.owner, world.ws), write),
        (token(world.partner, world.ws), read),
        (
            token(
                world.reader,
                world.ws,
                delegation=world.read_delegation,
                obo=world.owner,
            ),
            read,
        ),
        (
            token(
                world.reader,
                world.ws,
                delegation=world.read_delegation,
                obo=world.owner,
            ),
            write,
        ),
        (
            token(
                world.reader,
                world.ws,
                delegation=world.read_delegation,
                obo=world.partner,
            ),
            read,
        ),
        (token(world.owner, world.other_ws), read),
        (token(world.owner, world.ws), {**read, "approval_id": str(uuid.uuid4())}),
        ({**token(world.owner, world.ws), "x-empire-channel": "mcp"}, read),
        ({**token(world.owner, world.ws), "x-empire-channel": "fax"}, read),
        (token(world.owner, world.ws, audience=["tasks-api"]), read),
        ({}, read),
        (token(world.owner, world.ws), {**read, "action": "nope"}),
    ]
    for headers, body in cases:
        before = decisions_recorded(cluster)
        expected = primary.post("/v1/authz/decide", headers=headers, json=body)
        between = decisions_recorded(cluster)
        shadow = identity.post("/v1/authz/decide", headers=headers, json=body)
        assert decisions_recorded(cluster) == between, (
            "the shadow must not record decisions"
        )
        assert shadow.status_code == expected.status_code, (
            body,
            expected.text,
            shadow.text,
        )
        if expected.status_code == 200:
            assert between == before + 1
            want, got = expected.json(), shadow.json()
            assert (got["decision"], got["reason"]) == (
                want["decision"],
                want["reason"],
            )
            assert got["decision_id"] is None
        elif expected.status_code in (401, 403):
            assert shadow.json() == expected.json()


def test_google_login_exchanges_for_a_member_of_the_workspace(
    clients, google_keys, world, cluster
):
    _, identity = clients
    superuser(
        cluster,
        "UPDATE platform.identities SET external_subject = 'google:4242' WHERE id = %s",
        (world.partner,),
    )
    now = int(time.time())
    id_token = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": GOOGLE_CLIENT,
            "sub": "4242",
            "iat": now,
            "exp": now + 300,
        },
        google_keys[0],
        algorithm="RS256",
    )
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": id_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "audience": "tasks-api",
    }
    issued = identity.post("/v1/token", data={**form, "workspace": str(world.ws)})
    assert issued.status_code == 200, issued.text
    claims = jwt.decode(
        issued.json()["access_token"], options={"verify_signature": False}
    )
    assert (claims["sub"], claims["ws"], claims["iss"]) == (
        str(world.partner),
        str(world.ws),
        "https://identity.test",
    )
    refused = identity.post(
        "/v1/token", data={**form, "workspace": str(world.other_ws)}
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"
