"""MCP adapter: same authorization as the API, no escalation through arguments."""

from __future__ import annotations

import asyncio
import json

import pytest
from empire_mcp.server import build_server
from graph_world import (  # noqa: F401  (fixtures)
    apps,
    bearer,
    gkeys,
    gtoken,
    gworld,
    neighbors,
    run_publisher,
)
from mcp.server.mcpserver.exceptions import ToolError

IDENTITY_ARGUMENTS = [
    "actor",
    "workspace",
    "workspace_id",
    "delegation",
    "on_behalf_of",
    "token",
    "channel",
    "sub",
]


def server_for(apps, token: str):
    tasks, graph = apps
    return build_server(tasks, graph, lambda: token)


def call(server, name: str, arguments: dict):
    result = asyncio.run(server.call_tool(name, arguments))
    text = result.content[0].text
    return json.loads(text)


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        return conn.execute(query, params).fetchall()


def test_tools_take_no_identity_arguments(apps, gworld, gtoken):
    server = server_for(apps, gtoken(gworld.owner, gworld.ws))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "tasks.list",
        "tasks.get",
        "tasks.create",
        "tasks.update",
        "tasks.complete",
        "tasks.link",
        "graph.search",
        "graph.neighbors",
    }
    for tool in tools:
        assert tool.input_schema["additionalProperties"] is False
        assert not set(tool.input_schema.get("properties", {})) & set(
            IDENTITY_ARGUMENTS
        ), tool.name


@pytest.mark.parametrize("field", IDENTITY_ARGUMENTS)
def test_identity_arguments_are_rejected_not_ignored(apps, gworld, gtoken, field):
    agent = gtoken(
        gworld.coach, gworld.ws, delegation=gworld.coach_read, obo=gworld.owner
    )
    server = server_for(apps, agent)
    with pytest.raises(ToolError, match=f"invalid_arguments: {field}"):
        asyncio.run(
            server.call_tool(
                "tasks.create", {"title": "Escalate", field: str(gworld.owner)}
            )
        )


def test_mcp_and_api_reach_the_same_decisions(cluster, apps, gworld, gtoken):
    tasks, _ = apps
    owner = gtoken(gworld.owner, gworld.ws)
    existing = tasks.post(
        "/v1/tasks", json={"title": "Parity"}, headers=bearer(owner)
    ).json()
    cases = [
        (
            gtoken(
                gworld.coach, gworld.ws, delegation=gworld.coach_read, obo=gworld.owner
            ),
            "tasks.create",
            {"title": "Nope"},
            ("POST", "/v1/tasks", {"title": "Nope"}),
        ),
        (
            gtoken(gworld.viewer, gworld.ws),
            "tasks.complete",
            {"task_id": existing["id"]},
            ("POST", f"/v1/tasks/{existing['id']}/complete", None),
        ),
        (
            gtoken(gworld.outsider, gworld.beta),
            "tasks.get",
            {"task_id": existing["id"]},
            ("GET", f"/v1/tasks/{existing['id']}", None),
        ),
        (gtoken(gworld.coach, gworld.ws), "tasks.list", {}, ("GET", "/v1/tasks", None)),
    ]
    for token, tool, arguments, (method, path, body) in cases:
        api = tasks.request(method, path, headers=bearer(token), json=body)
        with pytest.raises(ToolError) as failure:
            asyncio.run(server_for(apps, token).call_tool(tool, arguments))
        assert str(api.status_code) in str(failure.value), (
            tool,
            api.status_code,
            failure.value,
        )


def test_agent_writes_through_mcp_are_attributed_to_its_delegation(
    cluster, apps, gworld, gtoken
):
    agent = gtoken(
        gworld.coach,
        gworld.ws,
        delegation=gworld.coach_rw,
        obo=gworld.owner,
        act=gworld.owner,
    )
    task = call(
        server_for(apps, agent),
        "tasks.create",
        {"title": "Long run 20 km", "priority": 2},
    )
    row = superuser(
        cluster,
        "SELECT e.initiated_by, e.executed_by, e.on_behalf_of, e.delegation_id, e.channel FROM tasks.tasks t"
        " JOIN platform.executions e ON e.id = t.created_by_execution WHERE t.id = %s",
        (task["id"],),
    )[0]
    assert row == (gworld.owner, gworld.coach, gworld.owner, gworld.coach_rw, "mcp")


def test_end_to_end_task_from_mcp_to_graph(cluster, apps, gworld, gtoken):
    """A task created and linked over MCP shows up in the Empire Graph, and only for permitted callers."""
    agent = gtoken(
        gworld.coach,
        gworld.ws,
        delegation=gworld.coach_rw,
        obo=gworld.owner,
        act=gworld.owner,
    )
    server = server_for(apps, agent)
    task = call(server, "tasks.create", {"title": "Book race hotel"})
    call(
        server,
        "tasks.link",
        {
            "task_id": task["id"],
            "relation": "supports-goal",
            "target_type": "goal",
            "target_ref": gworld.goal,
        },
    )
    run_publisher(cluster)

    seen = call(server, "graph.neighbors", {"urn": gworld.goal})["items"]
    assert {(item["urn"], item["relation"]) for item in seen} >= {
        (task["urn"], "supports-goal")
    }
    assert all(
        item["cell"] == "tasks" for item in seen
    )  # no finance or diary for the agent
    found = call(server, "graph.search", {"query": "race hotel"})["items"]
    assert [item["urn"] for item in found] == [task["urn"]]

    outsider = server_for(apps, gtoken(gworld.outsider, gworld.beta))
    assert call(outsider, "graph.neighbors", {"urn": gworld.goal})["items"] == []
    assert call(outsider, "graph.search", {"query": "race hotel"})["items"] == []
    _, graph = apps
    assert (task["urn"], "supports-goal", "in") in neighbors(
        graph, gtoken(gworld.owner, gworld.ws), gworld.goal
    )


def test_one_correlation_id_links_mcp_api_outbox_and_graph(
    cluster, apps, gworld, gtoken
):
    agent = gtoken(
        gworld.coach, gworld.ws, delegation=gworld.coach_rw, obo=gworld.owner
    )
    task = call(server_for(apps, agent), "tasks.create", {"title": "Traced task"})
    run_publisher(cluster)
    row = superuser(
        cluster,
        "SELECT e.correlation_id, o.correlation_id, i.correlation_id FROM tasks.tasks t"
        " JOIN platform.executions e ON e.id = t.created_by_execution"
        " JOIN tasks.outbox o ON o.execution_id = e.id"
        " JOIN graph.inbox i ON i.event_id = o.id WHERE t.id = %s",
        (task["id"],),
    )
    assert len(row) == 1
    execution, outbox, graph = row[0]
    assert execution.startswith("mcp-")
    assert execution == outbox == graph
