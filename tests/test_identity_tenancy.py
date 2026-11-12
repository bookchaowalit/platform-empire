"""platform-identity SPEC-006 end to end: the real identity service against migration 0009.

Sign-up, invitations, members, workspace status, delegated service tokens and "*" worker clients, through
identity's HTTP API as the identity_api role. Skipped when the ``platform_identity`` package is not installed.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from empire_platform.devtoken import key_pair

pytest.importorskip("platform_identity.tenancy", reason="platform-identity with SPEC-006 is not installed")

from platform_identity.app import ACCESS_TOKEN_TYPE, ID_TOKEN_TYPE, TOKEN_EXCHANGE, create_app  # noqa: E402
from platform_identity.auth import TokenVerifier  # noqa: E402
from platform_identity.google import GoogleVerifier  # noqa: E402
from platform_identity.keys import SigningKey, StaticKey  # noqa: E402
from platform_identity.service_clients import ASSERTION_TYPE, parse_service_clients  # noqa: E402
from platform_identity.tenancy import TenancyConfig  # noqa: E402

ISSUER = "https://identity.test"
GOOGLE = "client.apps.googleusercontent.com"
CODE = "beta-code-1"
OWNER = ["identity.member:read", "identity.member:admin", "identity.workspace:admin",
         "identity.installation:read", "decisions.decision:read", "decisions.decision:write"]
ROLES = {"finance": {"owner": tuple(OWNER),
                     "admin": tuple(p for p in OWNER if p != "identity.workspace:admin"),
                     "member": ("decisions.decision:read",)}}


@pytest.fixture(scope="module")
def world(cluster, tmp_path_factory):
    google_private, google_public = key_pair(tmp_path_factory.mktemp("g"))
    signing_private, _ = key_pair(tmp_path_factory.mktemp("s"))
    _, platform_public = key_pair(tmp_path_factory.mktemp("p"))
    commerce_private, commerce_public = key_pair(tmp_path_factory.mktemp("c"))
    worker_private, worker_public = key_pair(tmp_path_factory.mktemp("w"))
    commerce, worker = str(uuid.uuid4()), str(uuid.uuid4())
    clients = parse_service_clients(json.dumps({
        "commerce-api": {"subject": commerce, "workspace": "*", "audiences": ["tasks-api", "identity-api"],
                         "scopes": ["decisions.decision:read"], "public_key_pem": commerce_public,
                         "delegate_from": "commerce-api", "delegable_scopes": ["decisions.decision:read"],
                         "products": ["finance"]},
        "worker": {"subject": worker, "workspace": "*", "audiences": ["tasks-api"],
                   "scopes": ["decisions.decision:read"], "public_key_pem": worker_public, "products": ["finance"]},
    }))
    app = create_app(
        connect=lambda: cluster.connect("identity_api"),
        platform_verifier=TokenVerifier(StaticKey(platform_public), "empire-platform-test"),
        signing_key=SigningKey(signing_private),
        issuer=ISSUER,
        google=GoogleVerifier(StaticKey(google_public), frozenset({GOOGLE})),
        token_audiences=frozenset({"tasks-api", "commerce-api", "identity-api"}),
        cell_audiences=frozenset({"tasks-api"}),
        service_clients=clients,
        rate_limiter=type("Unlimited", (), {"allow": staticmethod(lambda key: True)})(),
        tenancy=TenancyConfig(
            invite_key=b"k" * 32, signup_products=frozenset({"finance"}), signup_mode="invite_code",
            signup_code_hashes=frozenset({hashlib.sha256(CODE.encode()).hexdigest()}),
            role_templates=ROLES, default_installs={"finance": ("commerce-api", "worker")}),
    )
    return {"http": TestClient(app), "google": google_private, "commerce": (commerce, commerce_private),
            "worker": (worker, worker_private), "signing": signing_private, "cluster": cluster}


def id_token(w, sub: str, email: str = "ann@example.com", verified: bool = True) -> str:
    now = int(time.time())
    return jwt.encode({"iss": "https://accounts.google.com", "aud": GOOGLE, "sub": sub, "email": email,
                       "email_verified": verified, "name": "Ann", "iat": now, "exp": now + 300},
                      w["google"], algorithm="RS256")


def assertion(client_id: str, private: str) -> str:
    now = int(time.time())
    return jwt.encode({"iss": client_id, "sub": client_id, "aud": f"{ISSUER}/v1/token", "iat": now,
                       "exp": now + 60, "jti": str(uuid.uuid4())}, private, algorithm="RS256")


def person_token(w, sub, ws, audience="identity-api", scope=None, email="ann@example.com"):
    data = {"grant_type": TOKEN_EXCHANGE, "subject_token": id_token(w, sub, email), "subject_token_type": ID_TOKEN_TYPE,
            "audience": audience, "workspace": ws}
    if scope:
        data["scope"] = scope
    return w["http"].post("/v1/token", data=data)


def bearer(response) -> dict[str, str]:
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def claims(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return jwt.decode(response.json()["access_token"], options={"verify_signature": False})


def new_tenant(w, sub=None):
    sub = sub or str(uuid.uuid4().int)[:20]
    r = w["http"].post("/v1/signup", json={"id_token": id_token(w, sub), "product": "finance",
                                           "slug": f"t-{uuid.uuid4().hex[:10]}", "name": "Books", "code": CODE})
    assert r.status_code == 201, r.text
    return sub, r.json()


def admin(w, sub, ws, scope="identity.member:admin identity.member:read"):
    return bearer(person_token(w, sub, ws, scope=scope))


def join(w, owner_sub, ws, role="member"):
    sub, email = str(uuid.uuid4().int)[:20], f"{uuid.uuid4().hex[:6]}@example.com"
    code = w["http"].post("/v1/invitations", headers=admin(w, owner_sub, ws), json={"email": email, "role": role})
    assert code.status_code == 201, code.text
    r = w["http"].post("/v1/invitations/accept", json={"id_token": id_token(w, sub, email), "code": code.json()["code"]})
    assert r.status_code == 200, r.text
    return sub, email


# --- sign-up -----------------------------------------------------------------------------------------


def test_signup_gates_and_replay(world):
    http, sub = world["http"], "11111111111111111"
    body = {"id_token": id_token(world, sub), "product": "finance", "slug": f"t-{uuid.uuid4().hex[:8]}", "name": "B"}
    assert http.post("/v1/signup", json=body).status_code == 403  # no code
    assert http.post("/v1/signup", json={**body, "code": "wrong"}).status_code == 403
    assert http.post("/v1/signup", json={**body, "code": CODE, "product": "crm"}).status_code == 403
    unverified = {**body, "code": CODE, "id_token": id_token(world, sub, verified=False)}
    assert http.post("/v1/signup", json=unverified).json()["error"] == "invalid_grant"
    first = http.post("/v1/signup", json={**body, "code": CODE})
    assert first.status_code == 201 and first.headers["cache-control"] == "no-store"
    again = http.post("/v1/signup", json={**body, "code": CODE, "slug": "another-slug"})
    assert again.status_code == 200 and again.json()["workspace_id"] == first.json()["workspace_id"]
    listed = http.post("/v1/me/workspaces", json={"id_token": id_token(world, sub)}).json()["workspaces"]
    assert [w["workspace_id"] for w in listed] == [first.json()["workspace_id"]]
    assert listed[0]["roles"] == ["owner"]
    # The subject is stored only as an HMAC.
    rows = world["cluster"].connect("postgres", "empire").execute(
        "SELECT subject_hmac FROM platform.signups WHERE workspace_id = %s", (first.json()["workspace_id"],)).fetchall()
    assert rows and sub not in rows[0][0]


# --- invitations, members, scopes (AC-07..AC-11, AC-15) -----------------------------------------------


def test_invite_accept_members_and_scope_checks(world):
    owner_sub, t = new_tenant(world)
    ws, http = t["workspace_id"], world["http"]
    member_sub, _ = join(world, owner_sub, ws)
    members = http.get("/v1/members", headers=admin(world, owner_sub, ws)).json()["members"]
    assert {m["kind"] for m in members} == {"human", "service_account"} and len(members) == 4
    # AC-15: a member asking for a permission it lacks is refused; what it holds is allowed.
    assert person_token(world, member_sub, ws, scope="identity.member:admin").status_code == 403
    assert person_token(world, member_sub, ws, audience="tasks-api", scope="decisions.decision:read").status_code == 200
    # A member token cannot administer; a token without identity-api audience is not accepted at all.
    member_token = bearer(person_token(world, member_sub, ws, scope="decisions.decision:read"))
    assert http.get("/v1/members", headers=member_token).status_code == 403
    tasks_token = bearer(person_token(world, owner_sub, ws, audience="tasks-api"))
    assert http.get("/v1/members", headers=tasks_token).status_code == 401
    # Suspend the member: its next exchange fails like a non-member's.
    member_id = next(m["identity_id"] for m in members if m["kind"] == "human" and m["roles"] == ["member"])
    assert http.patch(f"/v1/members/{member_id}", headers=admin(world, owner_sub, ws),
                      json={"status": "suspended"}).status_code == 200
    assert person_token(world, member_sub, ws, audience="tasks-api").json()["error"] == "invalid_grant"
    # The last owner cannot leave.
    owner_id = next(m["identity_id"] for m in members if m["roles"] == ["owner"])
    r = http.delete(f"/v1/members/{owner_id}", headers=admin(world, owner_sub, ws))
    assert (r.status_code, r.json()["error"]) == (409, "last_owner")
    # An invitation code is shown once and never stored in clear.
    out = http.post("/v1/invitations", headers=admin(world, owner_sub, ws), json={"email": "x@example.com",
                                                                                  "role": "admin"}).json()
    stored = world["cluster"].connect("postgres", "empire").execute(
        "SELECT code_hmac, email_hmac FROM platform.invitations WHERE id = %s", (out["invitation_id"],)).fetchone()
    assert out["code"] not in stored[0] and "x@example.com" not in stored[1]
    listed = http.get("/v1/invitations", headers=admin(world, owner_sub, ws)).json()["invitations"]
    assert all("code" not in i for i in listed)


def test_suspended_workspace_owner_can_only_reactivate(world):
    owner_sub, t = new_tenant(world)
    ws, http = t["workspace_id"], world["http"]
    owner = bearer(person_token(world, owner_sub, ws, scope="identity.workspace:admin"))
    assert http.patch("/v1/workspace", headers=owner, json={"status": "suspended"}).status_code == 200
    assert person_token(world, owner_sub, ws, audience="tasks-api").json()["error"] == "invalid_grant"
    assert person_token(world, owner_sub, ws, scope="identity.member:read").json()["error"] == "invalid_grant"
    again = bearer(person_token(world, owner_sub, ws, scope="identity.workspace:admin"))
    assert http.patch("/v1/workspace", headers=again, json={"status": "active"}).status_code == 200
    assert person_token(world, owner_sub, ws, audience="tasks-api").status_code == 200


# --- delegation (AC-16..AC-19) ------------------------------------------------------------------------


def delegate(world, subject_token: str, audience="tasks-api", scope="decisions.decision:read", client="commerce"):
    client_id = "commerce-api" if client == "commerce" else client
    return world["http"].post("/v1/token", data={
        "grant_type": TOKEN_EXCHANGE, "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN_TYPE,
        "audience": audience, "scope": scope, "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion(client_id, world["commerce"][1] if client == "commerce" else world["worker"][1])})


def test_delegated_token_keeps_workspace_and_initiator(world):
    owner_sub, t = new_tenant(world)
    ws = t["workspace_id"]
    subject = person_token(world, owner_sub, ws, audience="commerce-api",
                           scope="decisions.decision:read decisions.decision:write").json()["access_token"]
    out = claims(delegate(world, subject))
    assert out["sub"] == world["commerce"][0] and out["ws"] == ws
    assert out["act"] == {"sub": t["identity_id"]} and out["via"] == ["commerce-api"]
    assert out["aud"] == ["tasks-api", "platform-api"] and out["scope"] == "decisions.decision:read"
    assert out["exp"] <= jwt.decode(subject, options={"verify_signature": False})["exp"]
    # AC-19: the platform records the service account as executor and the person as initiator.
    with world["cluster"].connect("platform_api") as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (ws,))
        conn.execute("SELECT platform.begin_execution(%s, %s, %s, NULL, NULL, 'api', NULL)",
                     (t["identity_id"], out["sub"], out["sub"]))
        conn.rollback()


def test_delegation_only_narrows(world):
    owner_sub, t = new_tenant(world)
    ws = t["workspace_id"]
    read_only = person_token(world, owner_sub, ws, audience="commerce-api",
                             scope="decisions.decision:read").json()["access_token"]
    unscoped = person_token(world, owner_sub, ws, audience="commerce-api").json()["access_token"]
    other_aud = person_token(world, owner_sub, ws, audience="tasks-api",
                             scope="decisions.decision:read").json()["access_token"]
    assert delegate(world, read_only, scope="decisions.decision:write").json()["error"] == "invalid_scope"
    assert delegate(world, unscoped).json()["error"] == "invalid_scope"
    assert delegate(world, other_aud).json()["error"] == "invalid_grant"
    assert delegate(world, read_only, audience="identity-api").json()["error"] == "invalid_target"
    forged = jwt.encode({"iss": ISSUER, "aud": "commerce-api", "sub": t["identity_id"], "ws": ws,
                         "scope": "decisions.decision:read", "iat": int(time.time()), "exp": int(time.time()) + 60,
                         "jti": "x"}, world["google"], algorithm="RS256")  # not identity's key
    assert delegate(world, forged).json()["error"] == "invalid_grant"
    # The worker is not a delegating client.
    assert delegate(world, read_only, client="worker").json()["error"] == "unauthorized_client"
    # A worker's own client_credentials token is not a person-origin chain.
    worker_token = world["http"].post("/v1/token", data={
        "grant_type": "client_credentials", "audience": "tasks-api", "workspace": ws,
        "client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion("worker", world["worker"][1])})
    assert worker_token.status_code == 200, worker_token.text


def test_removed_initiator_or_uninstalled_client_stops_the_chain(world):
    owner_sub, t = new_tenant(world)
    ws, http = t["workspace_id"], world["http"]
    member_sub, _ = join(world, owner_sub, ws)
    subject = person_token(world, member_sub, ws, audience="commerce-api",
                           scope="decisions.decision:read").json()["access_token"]
    assert delegate(world, subject).status_code == 200
    member_id = next(m["identity_id"] for m in http.get("/v1/members", headers=admin(world, owner_sub, ws))
                     .json()["members"] if m["roles"] == ["member"])
    assert http.delete(f"/v1/members/{member_id}", headers=admin(world, owner_sub, ws)).status_code == 204
    assert delegate(world, subject).json()["error"] == "invalid_grant"
    owner_subject = person_token(world, owner_sub, ws, audience="commerce-api",
                                 scope="decisions.decision:read").json()["access_token"]
    assert http.delete(f"/v1/members/{world['commerce'][0]}", headers=admin(world, owner_sub, ws)).status_code == 204
    assert delegate(world, owner_subject).json()["error"] == "invalid_grant"


# --- "*" workers and installations (AC-20, AC-21, AC-23) ---------------------------------------------


def test_star_worker_needs_installation_and_lists_them(world):
    _, t = new_tenant(world)
    other_sub, other = new_tenant(world)
    http, ws = world["http"], t["workspace_id"]

    def worker_token(workspace=None):
        data = {"grant_type": "client_credentials", "audience": "tasks-api", "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": assertion("worker", world["worker"][1])}
        if workspace:
            data["workspace"] = workspace
        return http.post("/v1/token", data=data)

    assert claims(worker_token(ws))["ws"] == ws
    assert worker_token().json()["error"] == "invalid_request"
    # Uninstall from the other tenant, then it is refused there.
    owner2 = admin(world, other_sub, other["workspace_id"])
    assert http.delete(f"/v1/members/{world['worker'][0]}", headers=owner2).status_code == 204
    assert worker_token(other["workspace_id"]).json()["error"] == "invalid_grant"
    page = http.post("/v1/service/installations", data={
        "client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion("worker", world["worker"][1])})
    assert page.status_code == 200 and ws in page.json()["workspaces"]
    assert other["workspace_id"] not in page.json()["workspaces"]
    # Reinstall through the admin API (AC-23).
    r = http.post("/v1/installations", headers=owner2, json={"client_id": "worker"})
    assert r.status_code == 201, r.text
    assert worker_token(other["workspace_id"]).status_code == 200

