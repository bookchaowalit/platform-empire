"""Token exchange prototype: one audience per token, narrowing scopes, MCP uses it."""

from __future__ import annotations

import asyncio
import json

import jwt
import pytest
from empire_mcp.server import build_server
from graph_world import ISSUER, apps, bearer, gkeys, gtoken, gworld  # noqa: F401  (fixtures)

from empire_platform.devtoken import mint
from empire_platform.token_exchange import ExchangeRefused, TokenExchanger


@pytest.fixture
def exchanger(gkeys):
    private_pem, public_pem = gkeys
    return TokenExchanger(private_pem, public_pem, ISSUER)


@pytest.fixture
def subject(gkeys, gworld):
    private_pem, _ = gkeys

    def make(sub=None, **kwargs) -> str:
        return mint(
            private_pem,
            sub=sub or gworld.owner,
            ws=gworld.ws,
            issuer=ISSUER,
            audience="empire-mcp",
            **kwargs,
        )

    return make


def test_subject_token_is_not_accepted_by_cells(apps, subject):
    tasks, graph = apps
    token = subject()
    assert tasks.get("/v1/tasks", headers=bearer(token)).status_code == 401
    assert (
        graph.get(
            "/v1/graph/search", params={"q": "x"}, headers=bearer(token)
        ).status_code
        == 401
    )


def test_exchanged_token_works_only_for_its_audience(apps, subject, exchanger, gworld):
    tasks, graph = apps
    for_tasks = exchanger.exchange(subject(), "tasks-api")
    for_graph = exchanger.exchange(subject(), "graph-api")
    assert tasks.get("/v1/tasks", headers=bearer(for_tasks)).status_code == 200
    assert (
        graph.get(
            "/v1/graph/search", params={"q": "x"}, headers=bearer(for_tasks)
        ).status_code
        == 401
    )
    assert (
        graph.get(
            "/v1/graph/search", params={"q": "x"}, headers=bearer(for_graph)
        ).status_code
        == 200
    )
    assert tasks.get("/v1/tasks", headers=bearer(for_graph)).status_code == 401
    claims = jwt.decode(for_tasks, options={"verify_signature": False})
    assert claims["aud"] == "tasks-api"
    assert claims["exp"] - claims["iat"] <= 300


def test_scopes_only_narrow(apps, subject, exchanger, gworld):
    tasks, _ = apps
    read_only = exchanger.exchange(subject(), "tasks-api", ("tasks.task:read",))
    assert tasks.get("/v1/tasks", headers=bearer(read_only)).status_code == 200
    denied = tasks.post("/v1/tasks", json={"title": "x"}, headers=bearer(read_only))
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "scope_insufficient"
    # A wide scope does not add permissions the caller lacks: the viewer still cannot write.
    viewer = exchanger.exchange(subject(sub=gworld.viewer), "tasks-api")
    assert (
        tasks.post("/v1/tasks", json={"title": "x"}, headers=bearer(viewer)).status_code
        == 403
    )
    with pytest.raises(ExchangeRefused):
        exchanger.exchange(subject(), "tasks-api", ("finance.budget:read",))


def test_delegation_and_actor_survive_the_exchange(subject, exchanger, gworld):
    token = subject(
        sub=gworld.coach, delegation=gworld.coach_rw, obo=gworld.owner, act=gworld.owner
    )
    claims = jwt.decode(
        exchanger.exchange(token, "tasks-api"), options={"verify_signature": False}
    )
    assert (claims["sub"], claims["del"], claims["obo"], claims["act"]["sub"]) == (
        str(gworld.coach),
        str(gworld.coach_rw),
        str(gworld.owner),
        str(gworld.owner),
    )


def test_exchange_refuses_bad_input(subject, exchanger, gtoken, gworld):
    with pytest.raises(ExchangeRefused):
        exchanger.exchange(subject(), "finance-api")
    with pytest.raises(ExchangeRefused):
        exchanger.exchange(
            gtoken(gworld.owner, gworld.ws), "tasks-api"
        )  # not a subject token
    with pytest.raises(ExchangeRefused):
        exchanger.exchange(subject(ttl=-10), "tasks-api")  # expired


def test_mcp_with_exchange_never_forwards_the_subject_token(apps, subject, exchanger):
    tasks, graph = apps
    seen: list[str] = []
    original = tasks.request

    def spy(method, url, **kwargs):
        seen.append(kwargs["headers"]["Authorization"])
        return original(method, url, **kwargs)

    tasks.request = spy  # type: ignore[method-assign]
    try:
        token = subject()
        server = build_server(tasks, graph, lambda: token, exchanger.exchange)
        result = asyncio.run(server.call_tool("tasks.create", {"title": "Exchanged"}))
        assert json.loads(result.content[0].text)["title"] == "Exchanged"
    finally:
        tasks.request = original  # type: ignore[method-assign]
    assert seen
    assert all(header != f"Bearer {token}" for header in seen)
    claims = jwt.decode(
        seen[0].removeprefix("Bearer "), options={"verify_signature": False}
    )
    assert claims["aud"] == "tasks-api"
