"""MCP adapter for the tasks and graph cells.

Every tool calls the cell's HTTP API with the caller's own platform token, so
MCP uses exactly the same authorization as the API: no tool reads the
database, and no tool accepts an actor, workspace, delegation, or token
argument. Unknown arguments are rejected rather than ignored, so a model
cannot change who it acts as by adding fields to a tool call.

    EMPIRE_TOKEN=... empire-mcp      # stdio, local development
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

URN = r"^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArgs(Strict):
    status: Literal["todo", "in_progress", "done", "blocked", "cancelled"] | None = None
    limit: int = Field(default=50, ge=1, le=200)


class TaskRef(Strict):
    task_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class CreateArgs(Strict):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    priority: int = Field(default=3, ge=1, le=5)
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    due_date: date | None = None


class UpdateArgs(TaskRef):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "blocked", "cancelled"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    due_date: date | None = None
    tags: list[str] | None = None


class LinkArgs(TaskRef):
    relation: Literal[
        "supports-goal", "belongs-to", "related-to", "depends-on", "part-of"
    ]
    target_type: Literal["goal", "project", "context", "skill", "task"]
    target_ref: str = Field(pattern=URN)


class SearchArgs(Strict):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=200)


class NeighborsArgs(Strict):
    urn: str = Field(pattern=URN)
    limit: int = Field(default=50, ge=1, le=500)


class ToolFailed(ToolError):
    """Reaches the MCP client as a tool error carrying only the API's error code."""


class StrictMCPServer(MCPServer):
    """MCPServer that validates arguments against a strict model before any tool runs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.argument_models: dict[str, type[Strict]] = {}

    async def list_tools(self):  # type: ignore[override]
        tools = await super().list_tools()
        for tool in tools:
            model = self.argument_models.get(tool.name)
            if model is not None:
                tool.input_schema = {
                    **model.model_json_schema(),
                    "additionalProperties": False,
                }
        return tools

    async def call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any], context: Any = None
    ):
        model = self.argument_models.get(name)
        if model is None:
            raise ToolFailed(f"unknown tool {name}")
        try:
            model.model_validate(arguments or {})
        except ValidationError as exc:
            fields = sorted(
                {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            )
            raise ToolFailed(f"invalid_arguments: {', '.join(fields)}") from exc
        return await super().call_tool(name, arguments, context)


STATUS_WORDS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid",
    429: "rate_limited",
}


def status_word(status: int) -> str:
    return STATUS_WORDS.get(status, "upstream_error" if status >= 500 else "error")


def build_server(
    tasks: httpx.Client,
    graph: httpx.Client | None,
    token: Callable[[], str] | None,
    exchange: Callable[[str, str], str] | None = None,
    *,
    mint: Callable[[str], str] | None = None,
    server: StrictMCPServer | None = None,
) -> StrictMCPServer:
    """Register the tools on ``server`` (a new stdio-style server by default).

    The bearer for each cell call comes from exactly one source: ``mint(audience)``
    (hosted mode: a fresh token for the authenticated caller), else
    ``exchange(token(), audience)``, else ``token()``. Without ``graph`` the
    graph tools are not registered.
    """
    if server is None:
        server = StrictMCPServer(
            "empire",
            instructions="Solo Empire tasks and graph. Acts as the authenticated caller only.",
        )

    def bearer_for(audience: str) -> str:
        if mint is not None:
            return mint(audience)
        assert token is not None, "build_server needs token or mint"
        return token() if exchange is None else exchange(token(), audience)

    def call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
        # One correlation ID per tool call, carried through API, outbox, and graph.
        bearer = bearer_for("tasks-api" if client is tasks else "graph-api")
        headers = {
            "Authorization": f"Bearer {bearer}",
            "X-Empire-Channel": "mcp",
            "X-Correlation-Id": f"mcp-{uuid.uuid4().hex}",
        }
        try:
            response = client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise ToolFailed("unavailable") from None
        if response.status_code >= 400:
            # Fixed wording only: upstream bodies may carry data the caller must not see in tool output or logs.
            raise ToolFailed(
                f"{response.status_code}: {status_word(response.status_code)}"
            )
        return response.json() if response.content else None

    def tool(name: str, model: type[Strict], description: str):
        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            server.argument_models[name] = model
            server.tool(name=name, description=description)(function)
            return function

        return register

    @tool("tasks.list", ListArgs, "List tasks in the caller's workspace.")
    def tasks_list(status: str | None = None, limit: int = 50) -> dict:
        params = {"limit": limit, **({"status": status} if status else {})}
        return call(tasks, "GET", "/v1/tasks", params=params)

    @tool("tasks.get", TaskRef, "Get one task by ID.")
    def tasks_get(task_id: str) -> dict:
        return call(tasks, "GET", f"/v1/tasks/{task_id}")

    @tool("tasks.create", CreateArgs, "Create a task.")
    def tasks_create(
        title: str,
        description: str | None = None,
        priority: int = 3,
        project: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
    ) -> dict:
        body = CreateArgs(
            title=title,
            description=description,
            priority=priority,
            project=project,
            tags=tags or [],
            due_date=due_date,
        ).model_dump(mode="json", exclude_none=True)
        return call(tasks, "POST", "/v1/tasks", json=body)

    @tool("tasks.update", UpdateArgs, "Change fields of a task.")
    def tasks_update(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        due_date: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        body = UpdateArgs(
            task_id=task_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            due_date=due_date,
            tags=tags,
        ).model_dump(mode="json", exclude_none=True, exclude={"task_id"})
        return call(tasks, "PATCH", f"/v1/tasks/{task_id}", json=body)

    @tool("tasks.complete", TaskRef, "Mark a task as done.")
    def tasks_complete(task_id: str) -> dict:
        return call(tasks, "POST", f"/v1/tasks/{task_id}/complete")

    @tool(
        "tasks.link",
        LinkArgs,
        "Link a task to another entity with a registered relation.",
    )
    def tasks_link(
        task_id: str, relation: str, target_type: str, target_ref: str
    ) -> dict:
        return call(
            tasks,
            "POST",
            f"/v1/tasks/{task_id}/links",
            json={
                "relation": relation,
                "target_type": target_type,
                "target_ref": target_ref,
            },
        )

    if graph is None:
        return server

    @tool("graph.search", SearchArgs, "Find entities the caller may see by label.")
    def graph_search(query: str, limit: int = 20) -> dict:
        return call(
            graph, "GET", "/v1/graph/search", params={"q": query, "limit": limit}
        )

    @tool(
        "graph.neighbors",
        NeighborsArgs,
        "Entities directly linked to one entity, filtered by permission.",
    )
    def graph_neighbors(urn: str, limit: int = 50) -> dict:
        return call(
            graph, "GET", "/v1/graph/neighbors", params={"urn": urn, "limit": limit}
        )

    return server


def main() -> None:
    tasks = httpx.Client(
        base_url=os.environ.get("EMPIRE_TASKS_API", "http://127.0.0.1:3601")
    )
    graph = httpx.Client(
        base_url=os.environ.get("EMPIRE_GRAPH_API", "http://127.0.0.1:3602")
    )
    server = build_server(tasks, graph, lambda: os.environ["EMPIRE_TOKEN"])
    server.run("stdio")
