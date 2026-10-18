"""Hosted token rotation: provider pushes over HTTP, due detection, no secret in output."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from empire_platform import hosted_rotate as hr
from empire_platform import rotate


def _jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).rstrip(b"=")
    return f"e30.{body.decode()}.sig"


PROJECTS = {
    "data": {
        "projects": {
            "edges": [
                {
                    "node": {
                        "id": "p1",
                        "name": "life-goals-api",
                        "environments": {
                            "edges": [{"node": {"id": "e1", "name": "production"}}]
                        },
                        "services": {
                            "edges": [{"node": {"id": "s1", "name": "goals-api"}}]
                        },
                    }
                }
            ]
        }
    }
}


class Recorder:
    def __init__(self, deployed: str | None) -> None:
        self.deployed = deployed
        self.vercel_state = "READY"
        self.vercel_failed: list = []
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, str(request.url), body))
        url = str(request.url)
        if "railway" in url:
            query = body["query"]
            if "projects" in query:
                return httpx.Response(200, json=PROJECTS)
            if "variables(" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {"variables": {"GOALS_PUBLISHER_TOKEN": self.deployed}}
                    },
                )
            if "variableUpsert" in query:
                return httpx.Response(200, json={"data": {"variableUpsert": True}})
        if "vercel" in url and "/v6/deployments" in url:
            return httpx.Response(
                200, json={"deployments": [{"uid": "d1", "name": "owner-web"}]}
            )
        if "vercel" in url and request.method == "POST" and "/v13/deployments" in url:
            return httpx.Response(200, json={"id": "dpl1"})
        if "vercel" in url and "/v13/deployments/dpl1" in url:
            return httpx.Response(200, json={"readyState": self.vercel_state})
        if "vercel" in url:
            return httpx.Response(200, json={"failed": self.vercel_failed})
        if "cloudflare" in url:
            return httpx.Response(200, json={"success": True})
        if "universal-auth/login" in url:
            return httpx.Response(200, json={"accessToken": "inf"})
        if "infisical" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)


TARGETS = [
    hr.HostedTarget(
        "goals-publisher",
        "railway",
        "GOALS_PUBLISHER_TOKEN",
        "life-goals-api",
        "goals-api",
    ),
    hr.HostedTarget("solo-web", "vercel", "EMPIRE_CELL_TOKEN", "owner-web"),
    hr.HostedTarget(
        "owner-mcp",
        "cloudflare",
        "EMPIRE_OWNER_MCP_TOKEN",
        worker="solo-empire-mcp-remote",
    ),
    hr.HostedTarget(
        "owner-mcp",
        "infisical",
        "EMPIRE_AUTOMATION_TOKEN_OWNER_MCP",
        path="/empire-platform",
    ),
]


def _apis(recorder: Recorder) -> tuple[hr.Apis, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    env = {
        "RAILWAY_API_TOKEN": "r",
        "VERCEL_TOKEN": "v",
        "CLOUDFLARE_API_TOKEN": "c",
        "CLOUDFLARE_ACCOUNT_ID": "acct",
        "INFISICAL_CLIENT_ID": "id",
        "INFISICAL_CLIENT_SECRET": "secret",
        "INFISICAL_PROJECT_ID": "proj",
    }
    return hr.build_apis(env, client), client


def test_contract_loads_for_both_rotations():
    hosted, within = hr.load_hosted_targets()
    assert within == 10
    providers = {t.provider for t in hosted}
    assert providers == {"railway", "vercel", "cloudflare", "infisical"}
    vercel = {t.automation: t.project for t in hosted if t.provider == "vercel"}
    assert vercel == {"solo-web": "owner-web", "accounts-web": "accounts-web"}
    legacy, _ = rotate.load_targets()
    assert {t.provider for t in legacy} == {"railway", "vercel"}


def test_fresh_railway_token_is_not_due_but_unreadable_providers_always_are():
    recorder = Recorder(_jwt(time.time() + 25 * 86400))
    apis, _ = _apis(recorder)
    due = hr.due_hosted(TARGETS, apis, 10, time.time(), force=False)
    assert "goals-publisher" not in due
    assert set(due) == {"solo-web", "owner-mcp"}


def test_expiring_railway_token_is_due():
    recorder = Recorder(_jwt(time.time() + 3 * 86400))
    apis, _ = _apis(recorder)
    due = hr.due_hosted(TARGETS[:1], apis, 10, time.time(), force=False)
    assert due == {"goals-publisher": "deployed token missing or expiring"}


def test_run_pushes_every_provider_and_never_prints_a_token(capsys):
    recorder = Recorder(None)
    apis, _ = _apis(recorder)
    token = _jwt(time.time() + 30 * 86400) + "SECRETPART"
    due = {"goals-publisher": "forced", "solo-web": "forced", "owner-mcp": "forced"}
    failures = hr.run(
        TARGETS, due, lambda names: dict.fromkeys(names, token), apis, False
    )
    assert failures == 0
    out = capsys.readouterr().out
    assert "SECRETPART" not in out
    sent = [(m, u) for m, u, _ in recorder.calls]
    assert any("variableUpsert" in b.get("query", "") for _, _, b in recorder.calls)
    assert any("/v10/projects/owner-web/env" in u for _, u in sent)
    assert any("/v13/deployments" in u for _, u in sent)
    assert any("/workers/scripts/solo-empire-mcp-remote/secrets" in u for _, u in sent)
    assert any(
        "/api/v3/secrets/raw/EMPIRE_AUTOMATION_TOKEN_OWNER_MCP" in u for _, u in sent
    )
    pushes = [json.loads(line) for line in out.splitlines() if '"push"' in line]
    assert len(pushes) == 4 and all(p["ok"] for p in pushes)


def test_dry_run_mints_nothing():
    recorder = Recorder(None)
    apis, _ = _apis(recorder)

    def mint(names):
        raise AssertionError("dry run must not mint")

    assert hr.run(TARGETS, {"solo-web": "forced"}, mint, apis, True) == 0


def test_missing_provider_credentials_count_as_failures(capsys):
    failures = hr.run(
        TARGETS[:1],
        {"goals-publisher": "forced"},
        lambda n: dict.fromkeys(n, "t.t.t"),
        hr.Apis(),
        False,
    )
    assert failures == 1
    assert '"ok": false' in capsys.readouterr().out


def test_main_requires_the_signing_key():
    with pytest.raises(SystemExit, match="EMPIRE_SIGNING_PRIVATE_KEY_PEM"):
        hr.main({})


def test_vercel_partial_failure_and_failed_build_are_failures(capsys):
    for field, value in (
        ("vercel_failed", [{"error": {"code": "x"}}]),
        ("vercel_state", "ERROR"),
    ):
        recorder = Recorder(None)
        setattr(recorder, field, value)
        apis, _ = _apis(recorder)
        failures = hr.run(
            TARGETS[1:2],
            {"solo-web": "forced"},
            lambda n: dict.fromkeys(n, "a.b.c"),
            apis,
            False,
        )
        assert failures == 1, field
    capsys.readouterr()


def test_provider_messages_never_reach_the_log(capsys):
    def leaky(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "projects" in body["query"]:
            return httpx.Response(200, json=PROJECTS)
        echoed = body["variables"]["input"]["value"]
        return httpx.Response(
            200,
            json={
                "errors": [
                    {"message": f"bad value {echoed}", "extensions": {"code": "BAD"}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(leaky))
    apis = hr.Apis(railway=hr.RailwayApi(client, "r"))
    failures = hr.run(
        TARGETS[:1],
        {"goals-publisher": "forced"},
        lambda n: dict.fromkeys(n, "SECRET.JWT.VALUE"),
        apis,
        False,
    )
    out = capsys.readouterr().out
    assert failures == 1
    assert "SECRET" not in out and "BAD" in out


def test_tokens_never_outlive_their_delegation():
    from datetime import UTC, datetime, timedelta

    from empire_platform.automations import token_ttl

    month = 30 * 86400
    assert token_ttl(month, None) == month
    soon = datetime.now(UTC) + timedelta(days=9)
    assert 8 * 86400 < token_ttl(month, soon) <= 9 * 86400
    assert token_ttl(month, datetime.now(UTC) + timedelta(days=90)) == month
    assert token_ttl(month, datetime.now(UTC) - timedelta(days=1)) == 60


def test_a_failed_infisical_login_fails_only_the_infisical_copies(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if "universal-auth/login" in str(request.url):
            return httpx.Response(401, json={"message": "bad"})
        return Recorder(None)(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apis = hr.build_apis(
        {
            "CLOUDFLARE_API_TOKEN": "c",
            "CLOUDFLARE_ACCOUNT_ID": "acct",
            "INFISICAL_CLIENT_ID": "id",
            "INFISICAL_CLIENT_SECRET": "secret",
            "INFISICAL_PROJECT_ID": "proj",
        },
        client,
    )
    assert apis.infisical is None and apis.cloudflare is not None
    minted: list[list[str]] = []

    def mint(names):
        minted.append(names)
        return dict.fromkeys(names, "a.b.c")

    failures = hr.run(TARGETS[2:], {"owner-mcp": "forced"}, mint, apis, False)
    # owner-mcp has an Infisical copy: it is not re-minted (that would revoke the live token).
    assert minted == [] and failures == 2
    out = capsys.readouterr().out
    assert '"provider": "infisical"' in out and "HTTP 401" in out


def test_an_access_token_skips_the_login():
    recorder = Recorder(None)
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    apis = hr.build_apis({"INFISICAL_TOKEN": "t", "INFISICAL_PROJECT_ID": "p"}, client)
    assert apis.infisical is not None
    assert not any("universal-auth" in u for _, u, _ in recorder.calls)


def test_only_fully_deliverable_automations_are_minted():
    recorder = Recorder(_jwt(time.time()))
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    apis = hr.build_apis({"RAILWAY_API_TOKEN": "r"}, client)
    minted: list[list[str]] = []

    def mint(names):
        minted.append(names)
        return dict.fromkeys(names, "a.b.c")

    due = {"goals-publisher": "forced", "solo-web": "forced"}
    failures = hr.run(TARGETS[:2], due, mint, apis, False)
    assert minted == [["goals-publisher"]]
    assert failures == 1
