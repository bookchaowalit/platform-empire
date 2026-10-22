"""Tasks cell API (contract tasks.v1).

Every request runs as the ``tasks_api`` database role, bound to the token's
workspace with ``SET LOCAL``. Writes follow one pattern:

1. a recorded, unlocked authorization check in its own transaction, so
   denials are audited even though nothing else happens;
2. one transaction that opens an execution, re-checks authorization with
   locks (``enforce``), changes the data, writes the outbox event and the
   audit event, and closes the execution.

Resources outside the caller's workspace or permissions answer 404, the
same as resources that do not exist.
"""

# No `from __future__ import annotations`: FastAPI resolves the dependency
# annotations declared inside create_app at runtime.
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import psycopg
from empire_authz import AuthorizationDenied, Request, Resource, authorize, enforce
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi import Path as Path_
from fastapi import Request as HttpRequest
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, ConfigDict, Field

from empire_platform.token_exchange import MCP_ISSUER
from tasks_api.auth import (
    HARD_MAX_LIFETIME_SECONDS,
    Principal,
    TokenVerifier,
    Unauthenticated,
)

Status = Literal["todo", "in_progress", "done", "blocked", "cancelled"]
Relation = Literal["supports-goal", "belongs-to", "related-to", "depends-on", "part-of"]
TargetType = Literal["goal", "project", "context", "skill", "task"]
TASK_COLUMNS = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "project",
    "tags",
    "category",
    "due_date",
    "completed_at",
    "progress_percent",
    "notes",
    "parent_task_id",
    "calendar_event_id",
    "started_at",
    "estimated_minutes",
    "actual_minutes",
    "reminder_at",
    "reminder_channels",
    "reminder_sent",
    "recurrence_rule",
    "is_recurring_instance",
    "recurrence_occurrence",
    "parent_recurring_id",
    "external_source",
    "external_id",
    "external_url",
    "archived_at",
    "archive_reason",
    "version",
    "created_at",
    "updated_at",
    "legacy_id",
)
# Parent references also as task numbers, so callers never need a second lookup.
TASK_FIELDS = ", ".join(TASK_COLUMNS) + (
    ", (SELECT p.legacy_id FROM tasks.tasks p WHERE p.id = tasks.tasks.parent_task_id) AS parent_number"
    ", (SELECT p.legacy_id FROM tasks.tasks p WHERE p.id = tasks.tasks.parent_recurring_id) AS parent_recurring_number"
)
# PATCH fields that narrower automation permissions may change.
STATUS_FIELDS = {"status", "due_date", "progress_percent"}
CALENDAR_FIELDS = {"calendar_event_id"}
CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
log = logging.getLogger("tasks_api")


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    status: Status = "todo"
    priority: int = Field(default=3, ge=1, le=5)
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    due_date: date | None = None
    notes: str | None = None
    parent_task_id: uuid.UUID | None = None
    calendar_event_id: str | None = None
    started_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    actual_minutes: int = Field(default=0, ge=0)
    reminder_at: datetime | None = None
    reminder_channels: str | None = None
    recurrence_rule: str | None = None
    external_url: str | None = None


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "blocked", "cancelled"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    project: str | None = None
    tags: list[str] | None = None
    category: str | None = None
    due_date: date | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    notes: str | None = None
    parent_task_id: uuid.UUID | None = None
    calendar_event_id: str | None = None
    started_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    actual_minutes: int | None = Field(default=None, ge=0)
    reminder_at: datetime | None = None
    reminder_channels: str | None = None
    recurrence_rule: str | None = None
    external_url: str | None = None


class Archive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class ReminderDelivered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_at: datetime
    channel: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")


class GenerateOccurrences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date


class ExternalIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    external_id: str = Field(min_length=1, max_length=200)


class UpsertResult(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]
    task: "Task"


class ReminderResult(BaseModel):
    outcome: Literal["recorded", "already_recorded"]


class OccurrenceResult(BaseModel):
    created: list["Task"]
    existing: int


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: Relation
    target_type: TargetType
    target_ref: str = Field(pattern=r"^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$")
    attributes: dict[str, Any] | None = Field(
        default=None, description="Replaces stored attributes when given"
    )


class Task(BaseModel):
    id: uuid.UUID
    urn: str
    number: int
    title: str
    description: str | None
    status: Status
    priority: int
    project: str | None
    tags: list[str]
    category: str | None
    due_date: date | None
    completed_at: datetime | None
    progress_percent: int
    notes: str | None
    parent_task_id: uuid.UUID | None
    parent_number: int | None = None
    calendar_event_id: str | None
    started_at: datetime | None
    estimated_minutes: int | None
    actual_minutes: int
    reminder_at: datetime | None
    reminder_channels: str | None
    reminder_sent: bool
    recurrence_rule: str | None
    is_recurring_instance: bool
    recurrence_occurrence: int | None
    parent_recurring_id: uuid.UUID | None
    parent_recurring_number: int | None = None
    external_source: str | None
    external_id: str | None
    external_url: str | None
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskList(BaseModel):
    items: list[Task]


class StatGroup(BaseModel):
    key: str | None
    count: int


class TaskStats(BaseModel):
    total: int
    groups: list[StatGroup]


class LinkItem(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task_number: int
    relation: Relation
    target_type: TargetType
    target_ref: str
    attributes: dict[str, Any]


class LinkList(BaseModel):
    items: list[LinkItem]


class AssignmentPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee_type: Literal["identity", "role", "team"]
    assignee_ref: str = Field(min_length=1, max_length=200)
    assignee_name: str | None = Field(default=None, max_length=200)
    assignment_type: Literal["assigned", "cc", "viewer"] = "assigned"


class Assignment(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task_number: int
    assignee_type: Literal["identity", "role", "team"]
    assignee_ref: str
    assignee_name: str | None
    assignment_type: Literal["assigned", "cc", "viewer"]


class AssignmentList(BaseModel):
    items: list[Assignment]


class AssignmentResult(BaseModel):
    outcome: Literal["created", "unchanged"]


class RoleSuggestionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=200)
    source_registry: str = Field(min_length=1, max_length=300)
    confidence: Literal["high", "medium", "low"]
    match_reason: str = Field(min_length=1, max_length=2000)
    review_status: Literal["suggested", "accepted", "dismissed"] | None = None


class RoleSuggestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=200)
    source_registry: str = Field(min_length=1, max_length=300)
    review_status: Literal["suggested", "accepted", "dismissed"]
    expected_status: Literal["suggested", "accepted", "dismissed"] | None = None


class RoleSuggestion(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task_number: int
    role_id: str
    confidence: Literal["high", "medium", "low"]
    match_reason: str
    review_status: Literal["suggested", "accepted", "dismissed"]
    source_registry: str
    updated_at: datetime


class RoleSuggestionList(BaseModel):
    items: list[RoleSuggestion]


class RoleSuggestionResult(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]


DependencyType = Literal[
    "finish_to_start", "start_to_start", "finish_to_finish", "start_to_finish"
]


class DependencyPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depends_on: int = Field(ge=0, description="Task number this task depends on")
    dependency_type: DependencyType = "finish_to_start"


class Dependency(BaseModel):
    task_number: int
    depends_on_number: int
    dependency_type: DependencyType


class DependencyList(BaseModel):
    items: list[Dependency]


class RemovalResult(BaseModel):
    outcome: Literal["removed", "absent"]


class TaskType(BaseModel):
    id: uuid.UUID
    number: int | None
    name: str
    description: str | None
    icon: str | None
    color: str | None
    default_status: str
    allowed_statuses: list[str]
    is_active: bool
    sort_order: int


class TaskTypeList(BaseModel):
    items: list[TaskType]


class Link(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    relation: Relation
    target_type: TargetType
    target_ref: str


class Problem(BaseModel):
    error: str
    reason: str | None = None


def urn(task_id: uuid.UUID) -> str:
    return f"empire:tasks:task:{task_id}"


def to_task(row: dict[str, Any]) -> Task:
    return Task(
        urn=urn(row["id"]),
        number=row["legacy_id"],
        parent_number=row.get("parent_number"),
        parent_recurring_number=row.get("parent_recurring_number"),
        **{k: row[k] for k in TASK_COLUMNS if k != "legacy_id"},
    )


def external_actor(caller: Principal) -> dict[str, str]:
    """The OAuth subject behind an MCP-issued token (``clerk:<user id>``); an ID, never a token."""
    return {"external_actor": caller.external_actor} if caller.external_actor else {}


def audit_log(**fields: Any) -> None:
    """One structured line per write decision; IDs and codes only, never content."""
    log.info(
        json.dumps(
            {"correlation_id": correlation_id.get(), **fields},
            default=str,
            sort_keys=True,
        )
    )


MAX_OCCURRENCES_PER_CALL = (
    366  # defence in depth: one call never generates more than this; a retry continues
)


def recurrence_step(rule: str | None) -> timedelta | None:
    """Same rule language as the SQLite-era recurring_gen.py."""
    if not rule:
        return None
    rule = rule.strip().lower()
    fixed = {
        "daily": 1,
        "every day": 1,
        "weekly": 7,
        "every week": 7,
        "monthly": 30,
        "every month": 30,
        "biweekly": 14,
        "every 2 weeks": 14,
        "every 3 months": 90,
    }
    if rule in fixed:
        return timedelta(days=fixed[rule])
    for pattern, days in (
        (r"every\s+(\d+)\s*months?", 30),
        (r"every\s+(\d+)\s*d", 1),
        (r"every\s+(\d+)\s*w", 7),
    ):
        match = re.match(pattern, rule)
        if match:
            try:
                step = timedelta(days=int(match.group(1)) * days)
            except OverflowError:
                return None
            return (
                step if step > timedelta(0) else None
            )  # "every 0 days" would never advance
    return None


def next_occurrence(
    parent: dict[str, Any], existing: list[dict[str, Any]], step: timedelta, until: date
) -> tuple[int, date]:
    """Highest occurrence so far and the next due date (recurring_gen.py semantics)."""
    if existing:
        latest = existing[-1]
        anchor = latest["due_date"] or (
            latest["completed_at"].date() if latest["completed_at"] else None
        )
        if anchor is not None:
            return latest["recurrence_occurrence"], anchor + step
        return latest["recurrence_occurrence"], until
    if parent["due_date"]:
        return 0, parent["due_date"] + step
    return 0, until


def row_hash(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()


def create_app(
    connect: Callable[[], psycopg.Connection[Any]],
    verifier: TokenVerifier,
) -> FastAPI:
    app = FastAPI(title="Tasks API", version="1.0.0", description=__doc__)

    @app.middleware("http")
    async def correlate(request: HttpRequest, call_next):
        """Carry X-Correlation-Id through the execution, outbox event, and logs."""
        given = request.headers.get("x-correlation-id", "")
        token = correlation_id.set(
            given if CORRELATION.match(given) else uuid.uuid4().hex
        )
        try:
            response = await call_next(request)
        finally:
            value = correlation_id.get()
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = value
        return response

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

    Caller = Annotated[Principal, Depends(principal)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            conn = connect()
        except (
            psycopg.OperationalError
        ) as exc:  # pool or server exhausted: fail fast, do not hang
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
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

    def request_for(
        caller: Principal,
        action: str,
        resource_id: str,
        approval: uuid.UUID | None = None,
    ) -> Request:
        return Request(
            actor=caller.actor,
            workspace=caller.workspace,
            action=action,
            resource=Resource("task", resource_id, caller.workspace),
            channel=caller.channel,
            delegation=caller.delegation,
            on_behalf_of=caller.on_behalf_of if caller.delegation else None,
            approval=approval,
        )

    def precheck(caller: Principal, request: Request, *, not_found: bool) -> None:
        """Recorded, unlocked decision in its own transaction."""
        if caller.scopes is not None and request.action not in caller.scopes:
            if not_found:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "scope_insufficient"},
            )
        with bound(caller) as conn:
            decision = authorize(conn, request)
            conn.commit()
        if decision.allowed:
            return
        if not_found:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        status = 409 if decision.decision == "require_approval" else 403
        raise HTTPException(
            status_code=status,
            detail={"error": decision.decision, "reason": decision.reason},
        )

    def load(
        conn: psycopg.Connection[DictRow], task_id: uuid.UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {TASK_FIELDS} FROM tasks.tasks WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return row

    @contextmanager
    def acting(
        caller: Principal, request: Request, idempotency_key: str | None
    ) -> Iterator[tuple[psycopg.Connection[DictRow], uuid.UUID]]:
        """Execution + locked re-check + the write, all in one transaction."""
        with bound(caller) as conn:
            # Locks the switch row: switching writes off waits for writes already under way.
            switch = conn.execute("SELECT tasks.writes_enabled() AS on").fetchone()
            assert switch is not None  # a SELECT without FROM always yields one row
            if not switch["on"]:
                raise HTTPException(
                    status_code=503, detail={"error": "writes_disabled"}
                )
            try:
                # The execution trigger refuses acting for someone without a live delegation.
                begun = conn.execute(
                    "SELECT platform.begin_execution(%s, %s, %s, %s, %s, %s, %s, %s) AS id",
                    (
                        caller.initiated_by,
                        caller.actor,
                        caller.on_behalf_of,
                        caller.delegation,
                        request.approval,
                        caller.channel,
                        idempotency_key,
                        correlation_id.get(),
                    ),
                ).fetchone()
                assert begun is not None  # a SELECT without FROM always yields one row
                execution = begun["id"]
                enforce(conn, request)
            except (
                AuthorizationDenied,
                psycopg.errors.InsufficientPrivilege,
            ) as denied:
                conn.rollback()
                with bound(caller) as audit:
                    # Record the denial outside the rolled-back transaction.
                    decision = authorize(audit, request)
                    audit.commit()
                if isinstance(denied, AuthorizationDenied):
                    decision = denied.decision
                elif (
                    decision.allowed
                ):  # the trigger refused although the PDP allows: fail closed
                    decision = type(decision)(
                        "deny", "delegation_invalid", decision.decision_id
                    )
                status = 409 if decision.decision == "require_approval" else 403
                audit_log(
                    event="tasks.write.denied",
                    action=request.action,
                    reason=decision.reason,
                    actor=caller.actor,
                    workspace=caller.workspace,
                    **external_actor(caller),
                )
                raise HTTPException(
                    status_code=status,
                    detail={"error": decision.decision, "reason": decision.reason},
                ) from denied
            try:
                yield conn, execution
            except psycopg.errors.ForeignKeyViolation as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "reference_not_found"}
                ) from exc
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(
                    status_code=409, detail={"error": "conflict"}
                ) from exc
            conn.execute(
                "SELECT platform.finish_execution(%s, 'succeeded')", (execution,)
            )
            conn.commit()
            audit_log(
                event="tasks.write",
                action=request.action,
                execution=execution,
                actor=caller.actor,
                workspace=caller.workspace,
                **external_actor(caller),
            )

    def emit(
        conn: psycopg.Connection[DictRow],
        caller: Principal,
        execution: uuid.UUID,
        event: str,
        task_id: uuid.UUID,
        before: dict | None,
        after: dict | None,
        extra: dict | None = None,
    ) -> None:
        """Outbox event and audit record in the same transaction as the change."""
        current = after or before or {}
        payload: dict[str, Any] = {
            "task": urn(task_id),
            # A delete bumps the version past the last state so consumers can order it.
            "version": current.get("version", 0) + (1 if after is None else 0),
            "deleted": after is None,
        }
        if after is not None:
            payload.update(
                title=after["title"],
                status=after["status"],
                archived=after["archived_at"] is not None,
            )
        payload.update(extra or {})
        event_type = event if "." in event else f"tasks.task.{event}"
        conn.execute(
            "INSERT INTO tasks.outbox (workspace_id, type, subject, execution_id, payload, correlation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                caller.workspace,
                event_type,
                urn(task_id),
                execution,
                json.dumps(payload, default=str),
                correlation_id.get(),
            ),
        )
        conn.execute(
            "SELECT platform.record_audit(%s, 'tasks', %s, 'task', %s, %s, %s)",
            (execution, event_type, str(task_id), row_hash(before), row_hash(after)),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def task_filters(
        status: Annotated[list[Status] | None, Query()] = None,
        external_source: Annotated[
            str | None, Query(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
        ] = None,
        no_external_source: Annotated[
            bool, Query(description="Only tasks created locally")
        ] = False,
        project: Annotated[str | None, Query(max_length=200)] = None,
        category: Annotated[str | None, Query(max_length=100)] = None,
        priority_max: Annotated[int | None, Query(ge=1, le=5)] = None,
        due_from: date | None = None,
        due_before: date | None = None,
        completed_from: datetime | None = None,
        completed_before: datetime | None = None,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
        reminder_due_before: datetime | None = None,
        missing_calendar_event: bool = False,
        numbers: Annotated[list[int] | None, Query(max_length=500)] = None,
        parent: Annotated[
            int | None, Query(ge=0, description="Children of this task number")
        ] = None,
        q: Annotated[
            str | None,
            Query(
                max_length=200,
                description="Case-insensitive text in title or description",
            ),
        ] = None,
        include_archived: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """One WHERE clause shared by the list and the stats endpoints."""
        where = (
            " WHERE (%(archived)s OR archived_at IS NULL)"
            " AND (%(status)s::text[] IS NULL OR status = ANY(%(status)s))"
            " AND (%(source)s::text IS NULL OR external_source = %(source)s)"
            " AND (NOT %(local)s OR external_source IS NULL)"
            " AND (%(project)s::text IS NULL OR project = %(project)s)"
            " AND (%(category)s::text IS NULL OR category = %(category)s)"
            " AND (%(priority_max)s::int IS NULL OR priority <= %(priority_max)s)"
            " AND (%(due_from)s::date IS NULL OR due_date >= %(due_from)s)"
            " AND (%(due)s::date IS NULL OR due_date < %(due)s)"
            " AND (%(completed_from)s::timestamptz IS NULL OR completed_at >= %(completed_from)s)"
            " AND (%(completed_before)s::timestamptz IS NULL OR completed_at < %(completed_before)s)"
            " AND (%(created_from)s::timestamptz IS NULL OR created_at >= %(created_from)s)"
            " AND (%(created_before)s::timestamptz IS NULL OR created_at < %(created_before)s)"
            " AND (%(reminder)s::timestamptz IS NULL OR (reminder_at <= %(reminder)s AND NOT reminder_sent))"
            " AND (NOT %(calendar)s OR calendar_event_id IS NULL)"
            " AND (%(numbers)s::bigint[] IS NULL OR legacy_id = ANY(%(numbers)s))"
            " AND (%(parent)s::bigint IS NULL OR parent_task_id ="
            "      (SELECT p.id FROM tasks.tasks p WHERE p.legacy_id = %(parent)s))"
            " AND (%(q)s::text IS NULL OR title ILIKE %(q)s OR description ILIKE %(q)s)"
        )
        like = (
            None
            if q is None
            else "%"
            + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        return where, {
            "archived": include_archived,
            "status": status,
            "source": external_source,
            "local": no_external_source,
            "project": project,
            "category": category,
            "priority_max": priority_max,
            "due_from": due_from,
            "due": due_before,
            "completed_from": completed_from,
            "completed_before": completed_before,
            "created_from": created_from,
            "created_before": created_before,
            "reminder": reminder_due_before,
            "calendar": missing_calendar_event,
            "numbers": numbers,
            "parent": parent,
            "q": like,
        }

    Filters = Annotated[tuple[str, dict[str, Any]], Depends(task_filters)]

    @app.get(
        "/v1/tasks",
        response_model=TaskList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_tasks(
        caller: Caller,
        filters: Filters,
        after: Annotated[
            int | None,
            Query(ge=0, description="Cursor: return tasks numbered above this"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> TaskList:
        """Filters cover what the SQLite-era readers and writers selected; ordered by task number."""
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        where, params = filters
        with bound(caller) as conn:
            rows = conn.execute(
                f"SELECT {TASK_FIELDS} FROM tasks.tasks{where}"
                " AND (%(after)s::bigint IS NULL OR legacy_id > %(after)s)"
                " ORDER BY legacy_id LIMIT %(limit)s",
                {**params, "after": after, "limit": limit},
            ).fetchall()
        return TaskList(items=[to_task(row) for row in rows])

    @app.get(
        "/v1/tasks/stats",
        response_model=TaskStats,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def task_stats(
        caller: Caller,
        filters: Filters,
        group_by: Literal["status", "priority", "project", "category"] | None = None,
    ) -> TaskStats:
        """Counts for dashboards without moving every task over the wire."""
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        where, params = filters
        with bound(caller) as conn:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- the WHERE text is a fixed template from task_filters; every value is bound as a named %(...)s parameter
            counted = conn.execute(
                f"SELECT count(*) AS n FROM tasks.tasks{where}", params
            ).fetchone()
            assert counted is not None  # an aggregate without GROUP BY yields one row
            total = counted["n"]
            groups: list[StatGroup] = []
            if group_by:
                rows = conn.execute(
                    f"SELECT {group_by}::text AS key, count(*) AS n FROM tasks.tasks{where}"
                    f" GROUP BY {group_by} ORDER BY n DESC, key",
                    params,
                ).fetchall()
                groups = [StatGroup(key=row["key"], count=row["n"]) for row in rows]
        return TaskStats(total=total, groups=groups)

    @app.get(
        "/v1/tasks/number/{number}",
        response_model=Task,
        responses={401: {"model": Problem}, 404: {"model": Problem}},
    )
    def get_by_number(caller: Caller, number: int) -> Task:
        with bound(caller) as conn:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- TASK_FIELDS is a module constant; the value is bound as a %s parameter
            row = conn.execute(
                f"SELECT {TASK_FIELDS} FROM tasks.tasks WHERE legacy_id = %s", (number,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        precheck(
            caller,
            request_for(caller, "tasks.task:read", str(row["id"])),
            not_found=True,
        )
        return to_task(row)

    @app.get(
        "/v1/tasks/external/{source}/{external_id}",
        response_model=Task,
        responses={401: {"model": Problem}, 404: {"model": Problem}},
    )
    def get_external(
        caller: Caller,
        source: Annotated[str, Path_(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")],
        external_id: Annotated[str, Path_(min_length=1, max_length=200)],
    ) -> Task:
        with bound(caller) as conn:
            row = conn.execute(
                f"SELECT {TASK_FIELDS} FROM tasks.tasks"
                " WHERE external_source = %s AND external_id = %s",
                (source, external_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        precheck(
            caller,
            request_for(caller, "tasks.task:read", str(row["id"])),
            not_found=True,
        )
        return to_task(row)

    @app.post(
        "/v1/tasks",
        status_code=201,
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def create_task(
        caller: Caller,
        body: TaskCreate,
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> Task:
        request = request_for(caller, "tasks.task:write", "*")
        precheck(caller, request, not_found=False)
        if idempotency_key:
            with bound(caller) as conn:
                previous = conn.execute(
                    "SELECT * FROM platform.find_execution(%s)", (idempotency_key,)
                ).fetchone()
                if previous and previous["status"] == "succeeded":
                    row = conn.execute(
                        f"SELECT {TASK_FIELDS} FROM tasks.tasks WHERE created_by_execution = %s",
                        (previous["id"],),
                    ).fetchone()
                    if row:
                        precheck(
                            caller,
                            request_for(caller, "tasks.task:read", str(row["id"])),
                            not_found=True,
                        )
                        response.status_code = 200
                        return to_task(row)
        with acting(caller, request, idempotency_key) as (conn, execution):
            values = body.model_dump()
            if values["status"] == "done":
                values["completed_at"] = datetime.now().astimezone()
            values.update(
                workspace_id=caller.workspace,
                created_by_execution=execution,
                updated_by_execution=execution,
            )
            columns = list(values)
            row = conn.execute(
                f"INSERT INTO tasks.tasks ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                f" RETURNING {TASK_FIELDS}",
                [values[c] for c in columns],
            ).fetchone()
            assert row is not None  # INSERT ... RETURNING without ON CONFLICT
            emit(conn, caller, execution, "created", row["id"], None, row)
        return to_task(row)

    @app.get(
        "/v1/tasks/{task_id}",
        response_model=Task,
        responses={401: {"model": Problem}, 404: {"model": Problem}},
    )
    def get_task(caller: Caller, task_id: uuid.UUID) -> Task:
        precheck(
            caller, request_for(caller, "tasks.task:read", str(task_id)), not_found=True
        )
        with bound(caller) as conn:
            return to_task(load(conn, task_id))

    def authorized_for(
        caller: Principal,
        task_id: uuid.UUID,
        action: str,
        approval: uuid.UUID | None = None,
    ) -> Request:
        """404 unless the task exists and is readable; then the action's precheck."""
        request = request_for(caller, action, str(task_id), approval)
        with bound(caller) as conn:
            load(conn, task_id)
        # Callers that may not read the task learn nothing about it.
        precheck(
            caller, request_for(caller, "tasks.task:read", str(task_id)), not_found=True
        )
        precheck(caller, request, not_found=False)
        return request

    def change(
        caller: Principal,
        task_id: uuid.UUID,
        action: str,
        event: str,
        apply: Callable[
            [psycopg.Connection[DictRow], dict[str, Any], uuid.UUID],
            dict[str, Any] | None,
        ],
        approval: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any] | None:
        request = authorized_for(caller, task_id, action, approval)
        with acting(caller, request, idempotency_key) as (conn, execution):
            before = load(conn, task_id, lock=True)
            after = apply(conn, before, execution)
            emit(conn, caller, execution, event, task_id, before, after, extra)
        return after

    def changed(after: dict[str, Any] | None) -> dict[str, Any]:
        """The row from an ``apply`` that returns ``before`` or an UPDATE ... RETURNING.

        Such an UPDATE targets the row ``load`` locked FOR UPDATE in the same
        transaction (one RLS policy covers every command), so it always returns it."""
        assert after is not None
        return after

    @app.patch(
        "/v1/tasks/{task_id}",
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
            412: {"model": Problem},
        },
    )
    def update_task(
        caller: Caller,
        task_id: uuid.UUID,
        body: TaskUpdate,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Task:
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail={"error": "empty_update"})

        def apply(conn, before, execution):
            if if_match is not None and str(before["version"]) != if_match.strip('"'):
                raise HTTPException(
                    status_code=412, detail={"error": "version_mismatch"}
                )
            values = dict(updates)
            if (
                "status" in values and before["status"] == "done"
            ):  # reopening clears completion
                values["completed_at"] = None
            assignments = ", ".join(f"{column} = %s" for column in values)
            return conn.execute(
                f"UPDATE tasks.tasks SET {assignments}, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {TASK_FIELDS}",
                [*values.values(), execution, task_id],
            ).fetchone()

        if set(updates) <= STATUS_FIELDS:
            action = "tasks.status:write"
        elif set(updates) <= CALENDAR_FIELDS:
            action = "tasks.calendar:write"
        else:
            action = "tasks.task:write"
        return to_task(changed(change(caller, task_id, action, "updated", apply)))

    @app.post(
        "/v1/tasks/{task_id}/complete",
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def complete_task(caller: Caller, task_id: uuid.UUID) -> Task:
        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET status = 'done', completed_at = coalesce(completed_at, now()),"
                " progress_percent = 100, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {TASK_FIELDS}",
                (execution, task_id),
            ).fetchone()

        return to_task(
            changed(change(caller, task_id, "tasks.status:write", "completed", apply))
        )

    @app.delete(
        "/v1/tasks/{task_id}",
        status_code=204,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
            409: {"model": Problem},
        },
    )
    def delete_task(
        caller: Caller,
        task_id: uuid.UUID,
        x_empire_approval: Annotated[uuid.UUID | None, Header()] = None,
    ) -> Response:
        def apply(conn, before, execution):
            conn.execute("DELETE FROM tasks.tasks WHERE id = %s", (task_id,))

        change(
            caller,
            task_id,
            "tasks.task:delete",
            "deleted",
            apply,
            approval=x_empire_approval,
        )
        return Response(status_code=204)

    @app.post(
        "/v1/tasks/{task_id}/links",
        status_code=201,
        response_model=Link,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def link_task(caller: Caller, task_id: uuid.UUID, body: LinkCreate) -> Link:
        created: dict[str, Any] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "INSERT INTO tasks.task_links (workspace_id, task_id, relation, target_type, target_ref, attributes)"
                " VALUES (%s, %s, %s, %s, %s, COALESCE(%s::jsonb, '{}'::jsonb))"
                " ON CONFLICT (task_id, relation, target_ref) DO UPDATE"
                " SET attributes = COALESCE(%s::jsonb, tasks.task_links.attributes), updated_at = now()"
                " RETURNING id, task_id, relation, target_type, target_ref",
                (
                    caller.workspace,
                    task_id,
                    body.relation,
                    body.target_type,
                    body.target_ref,
                    None if body.attributes is None else json.dumps(body.attributes),
                    None if body.attributes is None else json.dumps(body.attributes),
                ),
            ).fetchone()
            created.update(row)
            return before

        change(
            caller,
            task_id,
            "tasks.link:write",
            "linked",
            apply,
            extra={
                "relation": body.relation,
                "target_type": body.target_type,
                "target_ref": body.target_ref,
            },
        )
        return Link(**created)

    @app.put(
        "/v1/tasks/{task_id}/external-identity",
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
            409: {"model": Problem},
        },
    )
    def set_external_identity(
        caller: Caller, task_id: uuid.UUID, body: ExternalIdentity
    ) -> Task:
        """Record the upstream identity of a task created here and pushed out by a sync.

        Idempotent for the same identity; a task already bound to a different one,
        or an identity already used by another task, is a conflict.
        """

        def apply(conn, before, execution):
            current = (before["external_source"], before["external_id"])
            wanted = (body.source, body.external_id)
            if current == wanted:
                return before
            if current != (None, None):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "conflict", "reason": "identity_already_set"},
                )
            return conn.execute(
                f"UPDATE tasks.tasks SET external_source = %s, external_id = %s, updated_by_execution = %s"
                f" WHERE id = %s RETURNING {TASK_FIELDS}",
                (body.source, body.external_id, execution, task_id),
            ).fetchone()

        return to_task(
            changed(change(caller, task_id, "tasks.task:write", "updated", apply))
        )

    @app.put(
        "/v1/tasks/external/{source}/{external_id}",
        response_model=UpsertResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            422: {"model": Problem},
        },
    )
    def upsert_external(
        caller: Caller,
        source: Annotated[str, Path_(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")],
        external_id: Annotated[str, Path_(min_length=1, max_length=200)],
        body: TaskCreate,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> UpsertResult:
        """Create or update the one task for an external record; retries never duplicate it.

        With If-Match (the version the caller last read), an existing task that was
        changed since is not overwritten: 412, so a sync never clobbers a local edit.
        """
        request = request_for(
            caller, "tasks.task:write", f"external:{source}:{external_id}"
        )
        precheck(caller, request, not_found=False)
        given = body.model_dump(exclude_unset=True)
        with acting(caller, request, None) as (conn, execution):
            values = body.model_dump()
            if values["status"] == "done":
                values["completed_at"] = datetime.now().astimezone()
            values.update(
                workspace_id=caller.workspace,
                external_source=source,
                external_id=external_id,
                created_by_execution=execution,
                updated_by_execution=execution,
            )
            columns = list(values)
            row = conn.execute(
                f"INSERT INTO tasks.tasks ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                " ON CONFLICT (workspace_id, external_source, external_id)"
                " WHERE external_source IS NOT NULL AND external_id IS NOT NULL DO NOTHING"
                f" RETURNING {TASK_FIELDS}",
                [values[c] for c in columns],
            ).fetchone()
            if row is not None:
                if if_match is not None:  # the caller expected an existing task
                    raise HTTPException(
                        status_code=412, detail={"error": "version_mismatch"}
                    )
                emit(conn, caller, execution, "created", row["id"], None, row)
                response.status_code = 201
                return UpsertResult(outcome="created", task=to_task(row))
            # Not guaranteed: a task hard-deleted between the conflict above and this
            # SELECT leaves None here (a 500). The cast keeps that behaviour as is.
            before = cast(
                DictRow,
                conn.execute(
                    f"SELECT {TASK_FIELDS} FROM tasks.tasks"
                    " WHERE external_source = %s AND external_id = %s FOR UPDATE",
                    (source, external_id),
                ).fetchone(),
            )
            if if_match is not None and str(before["version"]) != if_match.strip('"'):
                raise HTTPException(
                    status_code=412, detail={"error": "version_mismatch"}
                )
            changes = {k: v for k, v in given.items() if before.get(k) != v}
            if not changes:
                return UpsertResult(outcome="unchanged", task=to_task(before))
            if changes.get("status") == "done":
                changes["completed_at"] = (
                    before["completed_at"] or datetime.now().astimezone()
                )
            elif (
                "status" in changes and before["status"] == "done"
            ):  # reopened upstream
                changes["completed_at"] = None
            assignments = ", ".join(f"{column} = %s" for column in changes)
            after = conn.execute(
                f"UPDATE tasks.tasks SET {assignments}, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {TASK_FIELDS}",
                [*changes.values(), execution, before["id"]],
            ).fetchone()
            assert after is not None  # the row `before` locked FOR UPDATE above
            emit(conn, caller, execution, "updated", after["id"], before, after)
            return UpsertResult(outcome="updated", task=to_task(after))

    @app.post(
        "/v1/tasks/{task_id}/archive",
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def archive_task(caller: Caller, task_id: uuid.UUID, body: Archive) -> Task:
        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET archived_at = coalesce(archived_at, now()), archive_reason = %s,"
                f" updated_by_execution = %s WHERE id = %s RETURNING {TASK_FIELDS}",
                (body.reason, execution, task_id),
            ).fetchone()

        return to_task(
            changed(change(caller, task_id, "tasks.archive:write", "archived", apply))
        )

    @app.post(
        "/v1/tasks/{task_id}/unarchive",
        response_model=Task,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def unarchive_task(caller: Caller, task_id: uuid.UUID) -> Task:
        """Recovery path of the deletion policy: archived tasks come back intact."""

        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET archived_at = NULL, archive_reason = NULL, updated_by_execution = %s"
                f" WHERE id = %s RETURNING {TASK_FIELDS}",
                (execution, task_id),
            ).fetchone()

        return to_task(
            changed(change(caller, task_id, "tasks.archive:write", "unarchived", apply))
        )

    @app.post(
        "/v1/tasks/{task_id}/reminders/delivered",
        response_model=ReminderResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def reminder_delivered(
        caller: Caller, task_id: uuid.UUID, body: ReminderDelivered
    ) -> ReminderResult:
        """Record one delivery per reminder time and channel; notifier retries are no-ops."""
        request = authorized_for(caller, task_id, "tasks.reminder:write")
        with acting(caller, request, None) as (conn, execution):
            before = load(conn, task_id, lock=True)
            delivered = conn.execute(
                "INSERT INTO tasks.reminder_deliveries (workspace_id, task_id, reminder_at, channel, execution_id)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (task_id, reminder_at, channel) DO NOTHING RETURNING id",
                (caller.workspace, task_id, body.reminder_at, body.channel, execution),
            ).fetchone()
            if delivered is None:
                return ReminderResult(outcome="already_recorded")
            after = (
                conn.execute(
                    "UPDATE tasks.tasks SET reminder_sent = true, updated_by_execution = %s"
                    f" WHERE id = %s AND reminder_at = %s RETURNING {TASK_FIELDS}",
                    (execution, task_id, body.reminder_at),
                ).fetchone()
                or before
            )
            emit(
                conn,
                caller,
                execution,
                "tasks.reminder.delivered",
                task_id,
                before,
                after,
                {"reminder_at": body.reminder_at.isoformat(), "channel": body.channel},
            )
            return ReminderResult(outcome="recorded")

    @app.post(
        "/v1/tasks/{task_id}/occurrences/generate",
        response_model=OccurrenceResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
            422: {"model": Problem},
        },
    )
    def generate_occurrences(
        caller: Caller, task_id: uuid.UUID, body: GenerateOccurrences
    ) -> OccurrenceResult:
        """Create the due instances of a recurring task up to ``until``; retries create nothing new."""
        request = authorized_for(caller, task_id, "tasks.occurrence:write")
        with acting(caller, request, None) as (conn, execution):
            parent = load(conn, task_id, lock=True)
            step = recurrence_step(parent["recurrence_rule"])
            if step is None:
                raise HTTPException(
                    status_code=422, detail={"error": "no_recurrence_rule"}
                )
            existing = conn.execute(
                "SELECT recurrence_occurrence, due_date, completed_at FROM tasks.tasks"
                " WHERE parent_recurring_id = %s ORDER BY recurrence_occurrence",
                (task_id,),
            ).fetchall()
            occurrence, next_due = next_occurrence(parent, existing, step, body.until)
            created: list[Task] = []
            while next_due <= body.until and len(created) < MAX_OCCURRENCES_PER_CALL:
                occurrence += 1
                row = conn.execute(
                    "INSERT INTO tasks.tasks (workspace_id, title, description, priority, project, tags, category,"
                    " due_date, recurrence_rule, estimated_minutes, is_recurring_instance, recurrence_occurrence,"
                    " parent_recurring_id, created_by_execution, updated_by_execution)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s)"
                    " ON CONFLICT (parent_recurring_id, recurrence_occurrence) WHERE parent_recurring_id IS NOT NULL"
                    f" DO NOTHING RETURNING {TASK_FIELDS}",
                    (
                        caller.workspace,
                        f"[R{occurrence}] {parent['title']}",
                        parent["description"],
                        parent["priority"],
                        parent["project"],
                        parent["tags"],
                        parent["category"],
                        next_due,
                        parent["recurrence_rule"],
                        parent["estimated_minutes"],
                        occurrence,
                        task_id,
                        execution,
                        execution,
                    ),
                ).fetchone()
                if row is not None:
                    emit(conn, caller, execution, "created", row["id"], None, row)
                    created.append(to_task(row))
                try:
                    next_due = next_due + step
                except OverflowError:
                    break
            return OccurrenceResult(created=created, existing=len(existing))

    # ── relations: links, assignments, role suggestions, types ──────────
    def by_number(
        conn: psycopg.Connection[DictRow], number: int | None
    ) -> uuid.UUID | None:
        if number is None:
            return None
        row = conn.execute(
            "SELECT id FROM tasks.tasks WHERE legacy_id = %s", (number,)
        ).fetchone()
        return row["id"] if row else uuid.UUID(int=0)

    @app.get(
        "/v1/task-links",
        response_model=LinkList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_links(
        caller: Caller,
        task: Annotated[int | None, Query(ge=0, description="Task number")] = None,
        relation: Relation | None = None,
        target_type: TargetType | None = None,
        target_ref: Annotated[str | None, Query(max_length=300)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> LinkList:
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT l.id, l.task_id, t.legacy_id AS task_number, l.relation, l.target_type, l.target_ref,"
                " l.attributes FROM tasks.task_links l JOIN tasks.tasks t ON t.id = l.task_id"
                " WHERE (%(task)s::uuid IS NULL OR l.task_id = %(task)s)"
                " AND (%(relation)s::text IS NULL OR l.relation = %(relation)s)"
                " AND (%(type)s::text IS NULL OR l.target_type = %(type)s)"
                " AND (%(ref)s::text IS NULL OR l.target_ref = %(ref)s)"
                " ORDER BY t.legacy_id, l.created_at, l.id OFFSET %(offset)s LIMIT %(limit)s",
                {
                    "task": by_number(conn, task),
                    "relation": relation,
                    "type": target_type,
                    "ref": target_ref,
                    "offset": offset,
                    "limit": limit,
                },
            ).fetchall()
        return LinkList(items=[LinkItem(**row) for row in rows])

    @app.delete(
        "/v1/tasks/{task_id}/links",
        response_model=RemovalResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def unlink_task(
        caller: Caller,
        task_id: uuid.UUID,
        relation: Relation,
        target_ref: Annotated[
            str, Query(pattern=r"^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$")
        ],
    ) -> RemovalResult:
        """Withdraw a link; the graph retracts the edge and keeps its history."""
        outcome: dict[str, Literal["removed", "absent"]] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "DELETE FROM tasks.task_links WHERE task_id = %s AND relation = %s AND target_ref = %s"
                " RETURNING id",
                (task_id, relation, target_ref),
            ).fetchone()
            outcome["value"] = "removed" if row else "absent"
            return before

        change(
            caller,
            task_id,
            "tasks.link:write",
            "unlinked",
            apply,
            extra={"relation": relation, "target_ref": target_ref},
        )
        return RemovalResult(outcome=outcome["value"])

    @app.get(
        "/v1/task-assignments",
        response_model=AssignmentList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_assignments(
        caller: Caller,
        task: Annotated[int | None, Query(ge=0)] = None,
        assignee_type: Literal["identity", "role", "team"] | None = None,
        assignee_ref: Annotated[str | None, Query(max_length=200)] = None,
        assignment_type: Literal["assigned", "cc", "viewer"] | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> AssignmentList:
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT a.id, a.task_id, t.legacy_id AS task_number, a.assignee_type, a.assignee_ref,"
                " a.assignee_name, a.assignment_type FROM tasks.task_assignments a"
                " JOIN tasks.tasks t ON t.id = a.task_id"
                " WHERE (%(task)s::uuid IS NULL OR a.task_id = %(task)s)"
                " AND (%(type)s::text IS NULL OR a.assignee_type = %(type)s)"
                " AND (%(ref)s::text IS NULL OR a.assignee_ref = %(ref)s)"
                " AND (%(kind)s::text IS NULL OR a.assignment_type = %(kind)s)"
                " ORDER BY t.legacy_id, a.created_at, a.id OFFSET %(offset)s LIMIT %(limit)s",
                {
                    "task": by_number(conn, task),
                    "type": assignee_type,
                    "ref": assignee_ref,
                    "kind": assignment_type,
                    "offset": offset,
                    "limit": limit,
                },
            ).fetchall()
        return AssignmentList(items=[Assignment(**row) for row in rows])

    @app.put(
        "/v1/tasks/{task_id}/assignments",
        response_model=AssignmentResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def assign_task(
        caller: Caller, task_id: uuid.UUID, body: AssignmentPut
    ) -> AssignmentResult:
        """Idempotent: assigning the same assignee twice changes nothing."""
        outcome: dict[str, Literal["created", "unchanged"]] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "INSERT INTO tasks.task_assignments (workspace_id, task_id, assignee_type, assignee_ref, assignee_name,"
                " assignment_type) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (task_id, assignee_type, assignee_ref, assignment_type) DO NOTHING RETURNING id",
                (
                    caller.workspace,
                    task_id,
                    body.assignee_type,
                    body.assignee_ref,
                    body.assignee_name,
                    body.assignment_type,
                ),
            ).fetchone()
            outcome["value"] = "created" if row else "unchanged"
            return before

        change(
            caller,
            task_id,
            "tasks.assignment:write",
            "assigned",
            apply,
            extra={
                "assignee_type": body.assignee_type,
                "assignment_type": body.assignment_type,
            },
        )
        return AssignmentResult(outcome=outcome["value"])

    @app.get(
        "/v1/task-role-suggestions",
        response_model=RoleSuggestionList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_role_suggestions(
        caller: Caller,
        task: Annotated[int | None, Query(ge=0)] = None,
        role_id: Annotated[str | None, Query(max_length=200)] = None,
        source_registry: Annotated[str | None, Query(max_length=300)] = None,
        review_status: Literal["suggested", "accepted", "dismissed"] | None = None,
        confidence: Literal["high", "medium", "low"] | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> RoleSuggestionList:
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT s.id, s.task_id, t.legacy_id AS task_number, s.role_id, s.confidence, s.match_reason,"
                " s.review_status, s.source_registry, s.updated_at FROM tasks.task_role_suggestions s"
                " JOIN tasks.tasks t ON t.id = s.task_id"
                " WHERE (%(task)s::uuid IS NULL OR s.task_id = %(task)s)"
                " AND (%(role)s::text IS NULL OR s.role_id = %(role)s)"
                " AND (%(registry)s::text IS NULL OR s.source_registry = %(registry)s)"
                " AND (%(review)s::text IS NULL OR s.review_status = %(review)s)"
                " AND (%(confidence)s::text IS NULL OR s.confidence = %(confidence)s)"
                " ORDER BY t.legacy_id, s.role_id, s.id OFFSET %(offset)s LIMIT %(limit)s",
                {
                    "task": by_number(conn, task),
                    "role": role_id,
                    "registry": source_registry,
                    "review": review_status,
                    "confidence": confidence,
                    "offset": offset,
                    "limit": limit,
                },
            ).fetchall()
        return RoleSuggestionList(items=[RoleSuggestion(**row) for row in rows])

    @app.put(
        "/v1/tasks/{task_id}/role-suggestions",
        response_model=RoleSuggestionResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def suggest_role(
        caller: Caller, task_id: uuid.UUID, body: RoleSuggestionPut
    ) -> RoleSuggestionResult:
        """Upsert by (task, role, registry); a review decision already made is kept unless one is given."""
        outcome: dict[str, Literal["created", "updated", "unchanged"]] = {}

        def apply(conn, before, execution):
            current = conn.execute(
                "SELECT confidence, match_reason, review_status FROM tasks.task_role_suggestions"
                " WHERE task_id = %s AND role_id = %s AND source_registry = %s FOR UPDATE",
                (task_id, body.role_id, body.source_registry),
            ).fetchone()
            wanted = {
                "confidence": body.confidence,
                "match_reason": body.match_reason,
                "review_status": body.review_status
                or (current or {}).get("review_status")
                or "suggested",
            }
            if current is None:
                conn.execute(
                    "INSERT INTO tasks.task_role_suggestions (workspace_id, task_id, role_id, confidence, match_reason,"
                    " review_status, source_registry) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        caller.workspace,
                        task_id,
                        body.role_id,
                        wanted["confidence"],
                        wanted["match_reason"],
                        wanted["review_status"],
                        body.source_registry,
                    ),
                )
                outcome["value"] = "created"
            elif dict(current) != wanted:
                conn.execute(
                    "UPDATE tasks.task_role_suggestions SET confidence = %s, match_reason = %s, review_status = %s,"
                    " updated_at = now() WHERE task_id = %s AND role_id = %s AND source_registry = %s",
                    (
                        wanted["confidence"],
                        wanted["match_reason"],
                        wanted["review_status"],
                        task_id,
                        body.role_id,
                        body.source_registry,
                    ),
                )
                outcome["value"] = "updated"
            else:
                outcome["value"] = "unchanged"
            return before

        change(
            caller,
            task_id,
            "tasks.suggestion:write",
            "role-suggested",
            apply,
            extra={"role_id": body.role_id},
        )
        return RoleSuggestionResult(outcome=outcome["value"])

    @app.patch(
        "/v1/tasks/{task_id}/role-suggestions",
        response_model=RoleSuggestionResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def review_role(
        caller: Caller, task_id: uuid.UUID, body: RoleSuggestionReview
    ) -> RoleSuggestionResult:
        """Change the review status; with expected_status it only moves from that status (compare-and-set)."""
        outcome: dict[str, Literal["created", "updated", "unchanged"]] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "UPDATE tasks.task_role_suggestions SET review_status = %s, updated_at = now()"
                " WHERE task_id = %s AND role_id = %s AND source_registry = %s"
                " AND (%s::text IS NULL OR review_status = %s) AND review_status <> %s RETURNING id",
                (
                    body.review_status,
                    task_id,
                    body.role_id,
                    body.source_registry,
                    body.expected_status,
                    body.expected_status,
                    body.review_status,
                ),
            ).fetchone()
            outcome["value"] = "updated" if row else "unchanged"
            return before

        change(
            caller,
            task_id,
            "tasks.suggestion:write",
            "role-suggested",
            apply,
            extra={"role_id": body.role_id, "review_status": body.review_status},
        )
        return RoleSuggestionResult(outcome=outcome["value"])

    @app.delete(
        "/v1/tasks/{task_id}/role-suggestions",
        response_model=RemovalResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
        },
    )
    def withdraw_role(
        caller: Caller,
        task_id: uuid.UUID,
        role_id: Annotated[str, Query(min_length=1, max_length=200)],
        source_registry: Annotated[str, Query(min_length=1, max_length=300)],
        only_status: Literal["suggested", "accepted", "dismissed"] | None = None,
    ) -> RemovalResult:
        """Withdraw a generated suggestion; with only_status a reviewed one is never removed."""
        outcome: dict[str, Literal["removed", "absent"]] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "DELETE FROM tasks.task_role_suggestions WHERE task_id = %s AND role_id = %s AND source_registry = %s"
                " AND (%s::text IS NULL OR review_status = %s) RETURNING id",
                (task_id, role_id, source_registry, only_status, only_status),
            ).fetchone()
            outcome["value"] = "removed" if row else "absent"
            return before

        change(
            caller,
            task_id,
            "tasks.suggestion:write",
            "role-suggested",
            apply,
            extra={"role_id": role_id, "withdrawn": True},
        )
        return RemovalResult(outcome=outcome["value"])

    @app.get(
        "/v1/task-dependencies",
        response_model=DependencyList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_dependencies(
        caller: Caller,
        task: Annotated[int | None, Query(ge=0)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> DependencyList:
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT t.legacy_id AS task_number, d.legacy_id AS depends_on_number, x.dependency_type"
                " FROM tasks.task_dependencies x JOIN tasks.tasks t ON t.id = x.task_id"
                " JOIN tasks.tasks d ON d.id = x.depends_on_task_id"
                " WHERE (%(task)s::uuid IS NULL OR x.task_id = %(task)s)"
                " ORDER BY t.legacy_id, d.legacy_id OFFSET %(offset)s LIMIT %(limit)s",
                {"task": by_number(conn, task), "offset": offset, "limit": limit},
            ).fetchall()
        return DependencyList(items=[Dependency(**row) for row in rows])

    @app.put(
        "/v1/tasks/{task_id}/dependencies",
        response_model=AssignmentResult,
        responses={
            401: {"model": Problem},
            403: {"model": Problem},
            404: {"model": Problem},
            422: {"model": Problem},
        },
    )
    def add_dependency(
        caller: Caller, task_id: uuid.UUID, body: DependencyPut
    ) -> AssignmentResult:
        """Idempotent: the same dependency twice changes nothing."""
        outcome: dict[str, Literal["created", "unchanged"]] = {}

        def apply(conn, before, execution):
            target = by_number(conn, body.depends_on)
            if target is None or target == uuid.UUID(int=0) or target == task_id:
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_dependency"}
                )
            row = conn.execute(
                "INSERT INTO tasks.task_dependencies (workspace_id, task_id, depends_on_task_id, dependency_type)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (task_id, depends_on_task_id) DO NOTHING RETURNING id",
                (caller.workspace, task_id, target, body.dependency_type),
            ).fetchone()
            outcome["value"] = "created" if row else "unchanged"
            return before

        change(
            caller,
            task_id,
            "tasks.link:write",
            "dependency-added",
            apply,
            extra={"depends_on": body.depends_on},
        )
        return AssignmentResult(outcome=outcome["value"])

    @app.get(
        "/v1/task-types",
        response_model=TaskTypeList,
        responses={401: {"model": Problem}, 403: {"model": Problem}},
    )
    def list_task_types(caller: Caller, include_inactive: bool = False) -> TaskTypeList:
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                "SELECT id, legacy_id AS number, name, description, icon, color, default_status, allowed_statuses,"
                " is_active, sort_order FROM tasks.task_types WHERE (%s OR is_active)"
                " ORDER BY sort_order, name",
                (include_inactive,),
            ).fetchall()
        return TaskTypeList(items=[TaskType(**row) for row in rows])

    return app


DEV_ISSUER = "empire-platform-dev"
HOSTED_MAX_LIFETIME = HARD_MAX_LIFETIME_SECONDS  # delegated tokens only: each call re-checks delegation and revocation


def _pem_digest(pem: str) -> str:
    """SHA-256 of the canonical public key (DER SubjectPublicKeyInfo), so the same key in
    another encoding (PKCS#1, rewrapped lines) compares equal; unparsable text falls back
    to the PEM with whitespace removed."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_public_key(pem.strip().encode())
    except (ValueError, TypeError):
        try:  # PyJWT also accepts OpenSSH "ssh-rsa" keys: compare those canonically too
            key = serialization.load_ssh_public_key(pem.strip().encode())
        except (ValueError, TypeError, UnsupportedAlgorithm):
            return hashlib.sha256("".join(pem.split()).encode()).hexdigest()
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def _dev_key_digest() -> str | None:
    path = Path(__file__).resolve().parents[3] / ".runtime" / "dev-keys" / "public.pem"
    return _pem_digest(path.read_text(encoding="utf-8")) if path.is_file() else None


@dataclass(frozen=True)
class ServerConfig:
    public_key_pem: str
    issuer: str
    host: str
    port: int
    max_lifetime: int = 3600
    mcp_issuer: str | None = (
        None  # set together with mcp_public_key_pem when the hosted MCP server is trusted
    )
    mcp_public_key_pem: str | None = None
    mcp_subjects: frozenset[tuple[uuid.UUID, uuid.UUID]] | None = None
    # TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM: a second main-issuer key during a signing-key rollover.
    next_public_key_pem: str | None = None

    @property
    def main_public_key_pems(self) -> tuple[str, ...]:
        return (
            self.public_key_pem,
            *((self.next_public_key_pem,) if self.next_public_key_pem else ()),
        )


def server_config(env: Mapping[str, str] | None = None) -> ServerConfig:
    """Read the process settings; in hosted mode the dev issuer or a missing PEM is refused."""
    env = os.environ if env is None else env
    hosted = env.get("EMPIRE_DB_MODE") == "hosted"
    issuer = env.get("TASKS_API_JWT_ISSUER") or DEV_ISSUER
    pem = env.get("TASKS_API_JWT_PUBLIC_KEY_PEM", "").strip()
    if hosted and (not pem or issuer == DEV_ISSUER):
        raise SystemExit(
            "hosted mode needs TASKS_API_JWT_PUBLIC_KEY_PEM and a non-dev TASKS_API_JWT_ISSUER"
        )
    if hosted and _pem_digest(pem) == _dev_key_digest():
        raise SystemExit("hosted mode refuses the dev public key")
    if not pem:
        pem = Path(
            env.get("TASKS_API_JWT_PUBLIC_KEY", ".runtime/dev-keys/public.pem")
        ).read_text(encoding="utf-8")
    port = int(env.get("PORT") or env.get("TASKS_API_PORT") or "3601")
    lifetime = int(
        env.get("TASKS_API_JWT_MAX_LIFETIME_SECONDS")
        or (HOSTED_MAX_LIFETIME if hosted else 3600)
    )
    if not 0 < lifetime <= HARD_MAX_LIFETIME_SECONDS:
        raise SystemExit(
            f"TASKS_API_JWT_MAX_LIFETIME_SECONDS must be between 1 and {HARD_MAX_LIFETIME_SECONDS}"
        )
    mcp_issuer, mcp_pem = _mcp_trust(env, hosted, issuer, pem)
    next_pem = _next_key(env, hosted, pem, mcp_pem)
    # Ignored while MCP trust is off, so unsetting the key alone always cuts MCP off.
    mcp_subjects = _mcp_subjects(env.get("TASKS_API_MCP_SUBJECTS")) if mcp_pem else None
    return ServerConfig(
        pem,
        issuer,
        env.get("TASKS_API_HOST") or "127.0.0.1",
        port,
        lifetime,
        mcp_issuer,
        mcp_pem,
        mcp_subjects,
        next_pem,
    )


def _next_key(
    env: Mapping[str, str], hosted: bool, pem: str, mcp_pem: str | None
) -> str | None:
    """The rollover key verifies main-issuer tokens next to the current key; unset = off."""
    next_pem = env.get("TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM", "").strip()
    if not next_pem:
        return None
    if _pem_digest(next_pem) == _pem_digest(pem):
        raise SystemExit(
            "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM must differ from the current key"
        )
    if mcp_pem and _pem_digest(next_pem) == _pem_digest(mcp_pem):
        raise SystemExit(
            "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM must differ from the MCP key"
        )
    if hosted and _pem_digest(next_pem) == _dev_key_digest():
        raise SystemExit("hosted mode refuses the dev public key as the next key")
    return next_pem


def _mcp_subjects(raw: str | None) -> frozenset[tuple[uuid.UUID, uuid.UUID]] | None:
    """TASKS_API_MCP_SUBJECTS: JSON list of {"sub": identity uuid, "ws": workspace uuid}; unset = no extra gate."""
    if raw is None or not raw.strip():
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list) or not all(
            isinstance(e, dict) and set(e) == {"sub", "ws"} for e in data
        ):
            raise ValueError
        return frozenset(
            (uuid.UUID(str(e["sub"])), uuid.UUID(str(e["ws"]))) for e in data
        )
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise SystemExit(
            'TASKS_API_MCP_SUBJECTS must be a JSON list of {"sub": uuid, "ws": uuid}'
        ) from exc


def _mcp_trust(
    env: Mapping[str, str], hosted: bool, issuer: str, pem: str
) -> tuple[str | None, str | None]:
    """The hosted MCP issuer is trusted only when its own public key is configured."""
    mcp_pem = env.get("TASKS_API_MCP_PUBLIC_KEY_PEM", "").strip()
    if not mcp_pem:
        return None, None
    mcp_issuer = env.get("TASKS_API_TRUSTED_MCP_ISSUER") or MCP_ISSUER
    if mcp_issuer in (issuer, DEV_ISSUER):
        raise SystemExit(
            "TASKS_API_TRUSTED_MCP_ISSUER must differ from the main and the dev issuer"
        )
    if _pem_digest(mcp_pem) == _pem_digest(pem):
        raise SystemExit("the MCP public key must differ from the main public key")
    if hosted and _pem_digest(mcp_pem) == _dev_key_digest():
        raise SystemExit("hosted mode refuses the dev public key for MCP")
    return mcp_issuer, mcp_pem


def main() -> None:
    import uvicorn

    from empire_platform.devdb import default_cluster

    config = server_config()
    verifier = TokenVerifier(
        config.public_key_pem,
        config.issuer,
        max_lifetime=config.max_lifetime,
        mcp_issuer=config.mcp_issuer,
        mcp_public_key_pem=config.mcp_public_key_pem,
        mcp_subjects=config.mcp_subjects,
        extra_public_key_pems=config.main_public_key_pems[1:],
    )
    cluster = default_cluster()
    app = create_app(lambda: cluster.connect("tasks_api"), verifier)
    # Cells may verify tokens against this JWK Set instead of a pinned PEM (platform-identity step 1).
    from empire_platform import jwks

    jwks.register(app, *config.main_public_key_pems)
    # Decentralized cells (own database) reach the platform and graph over HTTP through this process.
    # Runs as tasks_api: it holds only the platform and graph entry points (authorize, cell executions,
    # audit, graph.ingest_cell_event), never platform or graph table grants.
    # platform-identity step 3: mirror answered decisions to the identity service when IDENTITY_SHADOW_URL is set.
    from platform_api.identity_shadow import IdentityShadow, enable_logging, shadow_url
    from platform_api.routes import register

    mirror_url = shadow_url(os.environ.get("IDENTITY_SHADOW_URL"))
    if mirror_url:
        enable_logging()
    register(
        app,
        connect_platform=lambda: cluster.connect("tasks_api"),
        platform_verifier=TokenVerifier(
            config.public_key_pem,
            config.issuer,
            audience="platform-api",
            max_lifetime=config.max_lifetime,
            extra_public_key_pems=config.main_public_key_pems[1:],
        ),
        graph_verifier=TokenVerifier(
            config.public_key_pem,
            config.issuer,
            audience="graph-api",
            max_lifetime=config.max_lifetime,
            extra_public_key_pems=config.main_public_key_pems[1:],
        ),
        identity_shadow=IdentityShadow(mirror_url) if mirror_url else None,
    )
    uvicorn.run(app, host=config.host, port=config.port)
