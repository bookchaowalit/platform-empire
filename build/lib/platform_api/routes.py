"""Platform over HTTP for cells with their own database (decentralized cells).

A cell such as ``solo-decisions`` keeps its data in its own Neon project, so it
cannot call ``platform.authorize`` or ``graph.ingest_event`` in its own
transaction the way the tasks cell does. These routes give it the same rules
over HTTP; the cell forwards the caller's bearer token, so every decision is
made, recorded, and attributed for the real caller, never for the cell.

- ``POST /v1/authz/decide``: a recorded, unlocked decision (reads, prechecks).
- ``POST /v1/executions``: start an execution and enforce the decision with
  locks in one platform transaction (writes). Idempotent per key.
- ``POST /v1/executions/{id}/finish``: the cell reports the outcome and the
  audit record (row hashes only, never row contents).
- ``POST /v1/graph/events``: a cell publisher delivers one outbox event; the
  publisher needs ``graph.<cell>:write`` where ``<cell>`` is the first segment
  of the event type, so a cell can only publish into its own namespace.

Tokens need the ``platform-api`` audience (``graph-api`` for events). The
cross-database write is not atomic: a cell that crashes between its commit and
``finish`` leaves the execution ``running``, which the monitor reports.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

import psycopg
from empire_authz import AuthorizationDenied, Request, Resource, authorize, enforce
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from tasks_api.auth import Principal, TokenVerifier, Unauthenticated

PERMISSION = re.compile(
    r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$"
)
CELL = re.compile(r"^[a-z][a-z0-9-]*$")
HASH = re.compile(r"^[0-9a-f]{64}$")
Auth = Annotated[str | None, Header()]
Channel = Annotated[str | None, Header(alias="x-empire-channel")]


class Decide(BaseModel):
    action: str = Field(pattern=PERMISSION.pattern)
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=200)
    approval_id: uuid.UUID | None = None


class Decision(BaseModel):
    decision: Literal["allow", "deny", "require_approval"]
    reason: str
    decision_id: uuid.UUID | None


class Begin(Decide):
    # Further permissions the same operation needs; all are enforced with locks in one transaction.
    also: list[str] = Field(default_factory=list, max_length=4)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    correlation_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:-]{8,128}$"
    )


class Execution(BaseModel):
    execution_id: uuid.UUID
    status: str
    replayed: bool = False


class Finish(BaseModel):
    status: Literal["succeeded", "failed"]
    service: str = Field(pattern=CELL.pattern, max_length=40)
    action: str = Field(min_length=1, max_length=120)
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=200)
    before_hash: str | None = Field(default=None, pattern=HASH.pattern)
    after_hash: str | None = Field(default=None, pattern=HASH.pattern)


class Event(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    type: str = Field(pattern=r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")
    subject: str = Field(pattern=r"^empire:[a-z0-9-]+:[a-z0-9-]+:.+$", max_length=300)
    version: int = Field(default=1, ge=0, le=2**53)
    classification: Literal["public", "internal", "confidential", "restricted"] = (
        "internal"
    )
    execution_id: uuid.UUID | None = None
    payload: dict[str, Any]
    occurred_at: str
    correlation_id: str | None = Field(default=None, max_length=64)


class Ingested(BaseModel):
    outcome: str


def register(
    app: FastAPI,
    *,
    connect_platform: Callable[[], psycopg.Connection],
    platform_verifier: TokenVerifier,
    graph_verifier: TokenVerifier,
    identity_shadow: Callable[..., None] | None = None,
) -> None:
    """Add the platform routes to an existing app (the tasks-api process hosts them).

    ``identity_shadow`` (see ``identity_shadow.py``) receives every answered decision after the response.
    """

    def caller(
        verifier: TokenVerifier, authorization: str | None, channel: str | None
    ) -> Principal:
        try:
            principal = verifier.verify(authorization, channel)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}
            ) from exc
        if (
            principal.external_actor is not None
        ):  # MCP-issued tokens reach cells through their own trust path
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "issuer_not_allowed"},
            )
        return principal

    @contextmanager
    def bound(
        principal: Principal, connect: Callable[[], psycopg.Connection]
    ) -> Iterator[psycopg.Connection]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
        conn.row_factory = dict_row
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(principal.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def request_for(principal: Principal, body: Decide) -> Request:
        return Request(
            actor=principal.actor,
            workspace=principal.workspace,
            action=body.action,
            resource=Resource(
                body.resource_type, body.resource_id, principal.workspace
            ),
            channel=principal.channel,
            delegation=principal.delegation,
            on_behalf_of=principal.on_behalf_of if principal.delegation else None,
            approval=body.approval_id,
        )

    def scoped(principal: Principal, action: str) -> None:
        if principal.scopes is not None and action not in principal.scopes:
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "scope_insufficient"},
            )

    @app.post("/v1/authz/decide", response_model=Decision, tags=["platform"])
    def decide(
        body: Decide,
        background: BackgroundTasks,
        authorization: Auth = None,
        x_empire_channel: Channel = None,
    ) -> Decision:
        principal = caller(platform_verifier, authorization, x_empire_channel)
        scoped(principal, body.action)
        with bound(principal, connect_platform) as conn:
            result = authorize(conn, request_for(principal, body))
            conn.commit()
        if identity_shadow is not None:
            background.add_task(
                identity_shadow,
                body.model_dump(mode="json", exclude_none=True),
                authorization,
                x_empire_channel,
                result.decision,
                result.reason,
            )
        return Decision(
            decision=result.decision,
            reason=result.reason,
            decision_id=result.decision_id,
        )

    @app.post(
        "/v1/executions",
        status_code=201,
        response_model=Execution,
        tags=["platform"],
        responses={403: {}, 409: {}},
    )
    def begin(
        body: Begin, authorization: Auth = None, x_empire_channel: Channel = None
    ) -> Execution:
        principal = caller(platform_verifier, authorization, x_empire_channel)
        if body.action.endswith(":read"):
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_request", "reason": "reads_use_decide"},
            )
        service = body.action.split(".", 1)[0]
        also = sorted(set(body.also) - {body.action})
        for action in [body.action, *also]:
            if (
                not PERMISSION.match(action)
                or action.split(".", 1)[0] != service
                or action.endswith(":read")
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_request", "reason": "bad_permission"},
                )
            scoped(principal, action)
        request = request_for(principal, body)
        extra = [
            request_for(principal, body.model_copy(update={"action": action}))
            for action in also
        ]
        args = (
            principal.initiated_by,
            principal.actor,
            principal.on_behalf_of,
            principal.delegation,
            body.approval_id,
            principal.channel,
            body.idempotency_key,
            body.correlation_id,
            service,
            body.action,
            body.resource_type,
            body.resource_id,
            also,
        )
        checking = request
        for attempt in range(
            2
        ):  # a concurrent first use of the same key is retried once as a replay
            with bound(principal, connect_platform) as conn:
                try:
                    started = conn.execute(
                        "SELECT execution_id, replayed, status FROM platform.begin_cell_execution("
                        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        args,
                    ).fetchone()
                    if started["replayed"]:
                        # The same operation again: answer only if all of it is still allowed now.
                        for checking in (request, *extra):
                            enforce(conn, checking)
                        conn.commit()
                        return Execution(
                            execution_id=started["execution_id"],
                            status=started["status"],
                            replayed=True,
                        )
                    for checking in (request, *extra):
                        enforce(conn, checking)
                    conn.commit()
                    return Execution(
                        execution_id=started["execution_id"], status="running"
                    )
                except psycopg.errors.UniqueViolation as exc:
                    conn.rollback()
                    if "idempotency_conflict" in str(exc) or attempt:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "conflict",
                                "reason": "idempotency_conflict",
                            },
                        ) from exc
                    continue
                except (
                    psycopg.errors.ForeignKeyViolation,
                    psycopg.errors.CheckViolation,
                ) as exc:
                    raise HTTPException(
                        status_code=422, detail={"error": "invalid_request"}
                    ) from exc
                except (
                    AuthorizationDenied,
                    psycopg.errors.InsufficientPrivilege,
                ) as denied:
                    conn.rollback()
                    with bound(principal, connect_platform) as audit:
                        # The permission that failed, recorded outside the rolled-back transaction.
                        decision = authorize(audit, checking)
                        audit.commit()
                    if isinstance(denied, AuthorizationDenied):
                        decision = denied.decision
                    elif decision.allowed:  # the execution trigger refused although the PDP allows: fail closed
                        decision = type(decision)(
                            "deny", "delegation_invalid", decision.decision_id
                        )
                    status = 409 if decision.decision == "require_approval" else 403
                    raise HTTPException(
                        status_code=status,
                        detail={"error": decision.decision, "reason": decision.reason},
                    ) from denied
        raise HTTPException(status_code=409, detail={"error": "conflict"})

    @app.post(
        "/v1/executions/{execution_id}/finish",
        response_model=Execution,
        tags=["platform"],
        responses={404: {}, 409: {}},
    )
    def finish(
        execution_id: uuid.UUID,
        body: Finish,
        authorization: Auth = None,
        x_empire_channel: Channel = None,
    ) -> Execution:
        principal = caller(platform_verifier, authorization, x_empire_channel)
        with bound(principal, connect_platform) as conn:
            # The same authority context the execution was started with (begin stores obo only with a delegation).
            outcome = conn.execute(
                "SELECT platform.finish_cell_execution(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS outcome",
                (
                    execution_id,
                    principal.actor,
                    principal.delegation,
                    principal.on_behalf_of,
                    sorted(principal.scopes) if principal.scopes is not None else None,
                    body.status,
                    body.service,
                    body.action,
                    body.resource_type,
                    body.resource_id,
                    body.before_hash,
                    body.after_hash,
                ),
            ).fetchone()["outcome"]
            conn.commit()
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        if outcome == "mismatch":
            raise HTTPException(
                status_code=409,
                detail={"error": "conflict", "reason": "operation_mismatch"},
            )
        if outcome != "finished":
            return Execution(execution_id=execution_id, status=outcome, replayed=True)
        return Execution(execution_id=execution_id, status=body.status)

    @app.post(
        "/v1/graph/events", response_model=Ingested, tags=["graph"], responses={403: {}}
    )
    def ingest(
        body: Event, authorization: Auth = None, x_empire_channel: Channel = None
    ) -> Ingested:
        principal = caller(graph_verifier, authorization, x_empire_channel or "event")
        if body.workspace_id != principal.workspace:
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "workspace_mismatch"},
            )
        cell = body.type.split(".", 1)[0]
        if (
            body.subject.split(":")[1] != cell
        ):  # a cell describes only its own nodes; links may point anywhere
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "subject_outside_cell"},
            )
        action = f"graph.{cell}:write"
        scoped(principal, action)
        request = request_for(
            principal,
            Decide(action=action, resource_type="graph-source", resource_id=cell),
        )
        event = json.dumps(body.model_dump(mode="json"))
        with bound(principal, connect_platform) as conn:
            decision = authorize(conn, request)
            conn.commit()
            if not decision.allowed:
                raise HTTPException(
                    status_code=403,
                    detail={"error": decision.decision, "reason": decision.reason},
                )
            # The commit ended the transaction that bound the workspace; bind it again for the ingest.
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(principal.workspace),),
            )
            try:
                # Re-checks workspace and namespace (after alias resolution) inside the ingest transaction.
                outcome = conn.execute(
                    "SELECT graph.ingest_cell_event(%s::jsonb, %s) AS outcome",
                    (event, cell),
                ).fetchone()["outcome"]
                conn.commit()
            except psycopg.errors.InsufficientPrivilege as exc:
                raise HTTPException(
                    status_code=403, detail={"error": "deny", "reason": "outside_cell"}
                ) from exc
            except (
                psycopg.errors.DataError,
                psycopg.errors.IntegrityError,
                psycopg.errors.RaiseException,
            ) as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_event"}
                ) from exc
        return Ingested(outcome=str(outcome))
