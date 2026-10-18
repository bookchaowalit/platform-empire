"""Python client for the shared policy decision point.

The rules live in ``platform.authorize`` (PostgreSQL); this module only maps
the language-neutral contract in ``contracts/authz/v1`` onto that call, so a
service in another language uses the same rules by calling the same function.

``authorize`` answers a question (for example before queueing work).
``enforce`` must run inside the transaction that performs the action: it
re-checks with row locks and raises unless the decision is ``allow``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

import psycopg
from psycopg.rows import tuple_row


@dataclass(frozen=True)
class Resource:
    type: str
    id: str
    workspace: uuid.UUID


@dataclass(frozen=True)
class Request:
    actor: uuid.UUID
    workspace: uuid.UUID
    action: str
    resource: Resource
    channel: str = "api"
    delegation: uuid.UUID | None = None
    on_behalf_of: uuid.UUID | None = None
    approval: uuid.UUID | None = None


@dataclass(frozen=True)
class Decision:
    decision: Literal["allow", "deny", "require_approval"]
    reason: str
    decision_id: uuid.UUID | None = field(default=None)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class AuthorizationDenied(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(f"{decision.decision}: {decision.reason}")
        self.decision = decision


def _call(conn: psycopg.Connection, request: Request, *, lock: bool) -> Decision:
    # Tuple rows regardless of the connection's row factory.
    row = (
        conn.cursor(row_factory=tuple_row)
        .execute(
            "SELECT decision, reason, decision_id FROM platform.authorize("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                request.actor,
                request.workspace,
                request.action,
                request.resource.type,
                request.resource.id,
                request.resource.workspace,
                request.delegation,
                request.on_behalf_of,
                request.approval,
                request.channel,
                lock,
            ),
        )
        .fetchone()
    )
    return Decision(row[0], row[1], row[2])


def authorize(conn: psycopg.Connection, request: Request) -> Decision:
    """Decide without locks; the decision is recorded in the audit trail."""
    return _call(conn, request, lock=False)


def enforce(conn: psycopg.Connection, request: Request) -> Decision:
    """Re-check with locks in the acting transaction; raise unless allowed."""
    decision = _call(conn, request, lock=True)
    if not decision.allowed:
        raise AuthorizationDenied(decision)
    return decision
