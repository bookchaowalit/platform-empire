"""Graph read API: permission-aware neighbors and search, and consent grants.

Every hop is filtered by the caller's permissions (and delegation) through
``platform.can``; anything the caller may not read is absent, including from
counts. A start node the caller cannot see returns an empty result, the same
as an unknown URN.
"""

# No `from __future__ import annotations`: FastAPI resolves annotations at runtime.
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field
from tasks_api.auth import Principal, TokenVerifier, Unauthenticated

URN = r"^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$"


class Node(BaseModel):
    urn: str
    cell: str
    type: str
    label: str | None
    classification: Literal["public", "internal", "confidential", "restricted"]


class Neighbor(Node):
    relation: str
    direction: Literal["in", "out"]


class Neighbors(BaseModel):
    urn: str
    items: list[Neighbor]


class Nodes(BaseModel):
    items: list[Node]


class ConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: str = Field(min_length=1)
    subject_urn: str = Field(pattern=URN)
    expires_at: datetime


def create_app(
    connect: Callable[[], psycopg.Connection], verifier: TokenVerifier
) -> FastAPI:
    app = FastAPI(title="Graph API", version="1.0.0", description=__doc__)

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_empire_channel: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return verifier.verify(authorization, x_empire_channel)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}
            ) from exc

    def scoped(caller: Principal = Depends(principal)) -> Principal:
        if caller.scopes is not None and "graph.query:read" not in caller.scopes:
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "scope_insufficient"},
            )
        return caller

    Caller = Annotated[Principal, Depends(scoped)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection]:
        conn = connect()
        conn.row_factory = dict_row
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(caller.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    @app.get("/v1/graph/neighbors", response_model=Neighbors)
    def neighbors(
        caller: Caller,
        urn: Annotated[str, Query(pattern=URN)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> Neighbors:
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT * FROM graph.neighbors(%s, %s, %s, %s)",
                (urn, caller.actor, caller.delegation, limit),
            ).fetchall()
        return Neighbors(urn=urn, items=[Neighbor(**row) for row in rows])

    @app.get("/v1/graph/search", response_model=Nodes)
    def search(
        caller: Caller,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Nodes:
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT * FROM graph.search(%s, %s, %s, %s)",
                (q, caller.actor, caller.delegation, limit),
            ).fetchall()
        return Nodes(items=[Node(**row) for row in rows])

    @app.post("/v1/graph/consents", status_code=201)
    def grant_consent(caller: Caller, body: ConsentCreate) -> dict[str, str]:
        """Only someone who may write the restricted entity's cell can consent to linking it."""
        cell, type_ = body.subject_urn.split(":")[1:3]
        with bound(caller) as conn:
            if (
                caller.delegation is not None
                or not conn.execute(
                    "SELECT platform.can(%s, %s, NULL) AS ok",
                    (caller.actor, f"{cell}.{type_}:write"),
                ).fetchone()["ok"]
            ):
                raise HTTPException(
                    status_code=403, detail={"error": "deny", "reason": "no_permission"}
                )
            row = conn.execute(
                "INSERT INTO graph.consents (workspace_id, granted_by, relation, subject_urn, expires_at)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    caller.workspace,
                    caller.actor,
                    body.relation,
                    body.subject_urn,
                    body.expires_at,
                ),
            ).fetchone()
            conn.commit()
        return {"id": str(row["id"])}

    return app
