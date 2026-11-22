"""SPEC-002 catalog routes: shared domains, values and metric definitions.

Registered by ``create_app`` (so ``main()`` serves them and ``empire-contracts`` includes them in the tasks
OpenAPI contract). Every route runs on the tasks_api role and calls one ``graph.catalog_*`` function; the
function re-checks permissions, so this module only adds the token-scope precheck the database cannot see
and maps the catalog's ``CT<status>`` errors to HTTP.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi import Path as Path_
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from tasks_api.auth import Principal

SLUG = r"^[a-z][a-z0-9-]{1,40}$"
KEY = r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*$"
Kind = Literal["domain", "value"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogProblem(BaseModel):
    model_config = ConfigDict(extra="allow")
    error: str
    reason: str | None = None


class ConceptRef(BaseModel):
    kind: str
    slug: str
    urn: str


class Concept(BaseModel):
    urn: str
    kind: str
    slug: str
    label: str
    description: str
    parent: str | None
    status: Literal["active", "merged", "split", "retired"]
    replaced_by: list[ConceptRef]
    introduced_in: int
    retired_in: int | None
    was: list[str]
    updated_at: datetime


class ConceptList(BaseModel):
    items: list[Concept]


class ConceptCreate(Strict):
    kind: Kind
    slug: str = Field(pattern=SLUG)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    parent: str | None = Field(default=None, pattern=SLUG)
    note: str = Field(min_length=1, max_length=2000)


class ConceptPatch(Strict):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    parent: str | None = Field(default=None, pattern=SLUG)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class MergeBody(Strict):
    into: str = Field(pattern=SLUG)
    note: str = Field(min_length=1, max_length=2000)


class SplitBody(Strict):
    into: list[Annotated[str, Field(pattern=SLUG)]] = Field(min_length=2, max_length=20)
    note: str = Field(min_length=1, max_length=2000)
    metric_moves: dict[str, Annotated[str, Field(pattern=SLUG)]] | None = None


class RetireBody(Strict):
    note: str = Field(min_length=1, max_length=2000)
    metric_moves: dict[str, Annotated[str, Field(pattern=SLUG)]] | None = None


class Merged(BaseModel):
    concept: Concept
    merged: Concept
    moved_metrics: list[str]
    version: int


class Retired(BaseModel):
    concept: Concept
    moved_metrics: list[str]
    version: int


class Metric(BaseModel):
    urn: str
    key: str
    label: str
    description: str
    domain: str
    domain_urn: str
    unit: str | None
    scale: Literal["nominal", "ordinal", "interval", "ratio"]
    value_range: dict[str, int | float] | None
    kind: Literal["leading", "lagging", "guardrail", "descriptive"]
    objectivity: Literal["objective", "subjective"]
    source: dict[str, Any]
    aggregation: str
    cadence: Literal["daily", "weekly", "monthly", "event"]
    criteria: list[dict[str, Any]]
    target: dict[str, Any] | None
    classification: Literal["public", "internal", "confidential", "restricted"]
    read_permission: str
    status: Literal["active", "retired"]
    replaced_by: str | None
    version: int
    updated_at: datetime


class MetricList(BaseModel):
    items: list[Metric]


class MetricPut(Strict):
    """A full definition; ``if_version`` is required when the key already exists (D3)."""

    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    domain: str = Field(pattern=SLUG)
    unit: str | None = Field(default=None, max_length=20)
    scale: Literal["nominal", "ordinal", "interval", "ratio"]
    value_range: dict[str, int | float] | None = None
    kind: Literal["leading", "lagging", "guardrail", "descriptive"]
    objectivity: Literal["objective", "subjective"]
    source: dict[str, Any]
    aggregation: Literal["count", "mode", "last", "median", "min", "max", "mean", "sum"]
    cadence: Literal["daily", "weekly", "monthly", "event"]
    criteria: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    target: dict[str, Any] | None = None
    classification: (
        Literal["public", "internal", "confidential", "restricted"] | None
    ) = None
    status: Literal["active", "retired"] = "active"
    replaced_by: str | None = Field(default=None, pattern=KEY)
    if_version: int | None = Field(default=None, ge=1)


class ReviewConcept(BaseModel):
    urn: str
    kind: str
    slug: str
    label: str
    status: str


class ReviewItem(BaseModel):
    concept: ReviewConcept
    cell: str
    type: str
    count: int
    recent_labels: list[str]


class Review(BaseModel):
    items: list[ReviewItem]


class SnapshotCount(BaseModel):
    cell: str
    type: str
    classification: str
    count: int


class TaxonomyVersion(BaseModel):
    version: int
    note: str
    created_by: uuid.UUID | None
    created_at: datetime
    restricted_excluded: bool
    unclassified: list[SnapshotCount]


class VersionList(BaseModel):
    items: list[TaxonomyVersion]


ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": CatalogProblem} for status in (401, 403, 404, 409, 422)
}


def register_catalog(
    app: FastAPI,
    connect: Callable[[], psycopg.Connection[Any]],
    principal: Callable[..., Principal],
) -> None:
    Caller = Annotated[Principal, Depends(principal)]
    Approval = Annotated[uuid.UUID | None, Header(alias="X-Empire-Approval")]

    def scoped(caller: Principal, permission: str, *, read: bool) -> None:
        """JWT scopes are invisible to the database: refuse here, before any query (D8)."""
        if caller.scopes is not None and permission not in caller.scopes:
            if read:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(
                status_code=403,
                detail={"error": "deny", "reason": "scope_insufficient"},
            )

    def caller_json(caller: Principal, approval: uuid.UUID | None = None) -> Jsonb:
        return Jsonb(
            {
                "actor": str(caller.actor),
                "delegation": str(caller.delegation) if caller.delegation else None,
                "on_behalf_of": str(caller.on_behalf_of) if caller.delegation else None,
                "scopes": sorted(caller.scopes) if caller.scopes is not None else None,
                "approval": str(approval) if approval else None,
                "channel": caller.channel,
            }
        )

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection[Any]]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(caller.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def run(caller: Principal, function: str, *args: Any) -> Any:
        placeholders = ", ".join(["%s"] * len(args))
        with bound(caller) as conn:
            try:
                row = conn.execute(
                    f"SELECT graph.{function}({placeholders})",  # nosemgrep: fixed names only
                    args,
                ).fetchone()
            except psycopg.Error as exc:
                conn.rollback()
                state = exc.sqlstate or ""
                if state.startswith("CT") and state[2:].isdigit():
                    detail = json.loads(exc.diag.message_detail or "{}")
                    raise HTTPException(
                        status_code=int(state[2:]),
                        detail={"error": exc.diag.message_primary or "error", **detail},
                    ) from None
                if isinstance(exc, psycopg.errors.DataError):
                    raise HTTPException(
                        status_code=422, detail={"error": "invalid_request"}
                    ) from None
                raise
            # A refused write is returned, not raised, so its decision record commits with it.
            conn.commit()
        result = row[0] if row else None
        if (
            isinstance(result, dict)
            and isinstance(result.get("status"), int)
            and "error" in result
        ):
            status = 409 if result["error"] == "require_approval" else result["status"]
            raise HTTPException(
                status_code=status,
                detail={"error": result["error"], "reason": result.get("reason")},
            )
        return result

    SlugPath = Annotated[str, Path_(pattern=SLUG)]

    @app.get(
        "/v1/catalog/concepts",
        response_model=ConceptList,
        tags=["catalog"],
        responses=ERRORS,
    )
    def list_concepts(
        caller: Caller,
        kind: Kind | None = None,
        status: Literal["active", "merged", "split", "retired"] | None = None,
    ) -> dict[str, Any]:
        scoped(caller, "catalog.concept:read", read=True)
        return {
            "items": run(
                caller, "catalog_list_concepts", caller_json(caller), kind, status
            )
        }

    @app.post(
        "/v1/catalog/concepts",
        response_model=Concept,
        status_code=201,
        tags=["catalog"],
        responses=ERRORS,
    )
    def create_concept(
        caller: Caller, body: ConceptCreate, approval: Approval = None
    ) -> Any:
        scoped(caller, "catalog.concept:write", read=False)
        return run(
            caller,
            "catalog_create_concept",
            caller_json(caller, approval),
            body.kind,
            body.slug,
            body.label,
            body.description,
            body.parent,
            body.note,
        )

    @app.patch(
        "/v1/catalog/concepts/{kind}/{slug}",
        response_model=Concept,
        tags=["catalog"],
        responses=ERRORS,
    )
    def update_concept(
        caller: Caller,
        kind: Kind,
        slug: SlugPath,
        body: ConceptPatch,
        approval: Approval = None,
    ) -> Any:
        scoped(caller, "catalog.concept:write", read=False)
        # only the fields sent: an explicit "parent": null clears the parent
        patch = body.model_dump(include=body.model_fields_set)
        return run(
            caller,
            "catalog_update_concept",
            caller_json(caller, approval),
            kind,
            slug,
            Jsonb(patch),
        )

    @app.post(
        "/v1/catalog/concepts/{kind}/{slug}/merge",
        response_model=Merged,
        tags=["catalog"],
        responses=ERRORS,
    )
    def merge_concept(
        caller: Caller,
        kind: Kind,
        slug: SlugPath,
        body: MergeBody,
        approval: Approval = None,
    ) -> Any:
        scoped(caller, "catalog.concept:admin", read=False)
        return run(
            caller,
            "catalog_merge",
            caller_json(caller, approval),
            kind,
            slug,
            body.into,
            body.note,
        )

    @app.post(
        "/v1/catalog/concepts/{kind}/{slug}/split",
        response_model=Retired,
        tags=["catalog"],
        responses=ERRORS,
    )
    def split_concept(
        caller: Caller,
        kind: Kind,
        slug: SlugPath,
        body: SplitBody,
        approval: Approval = None,
    ) -> Any:
        scoped(caller, "catalog.concept:admin", read=False)
        moves = Jsonb(body.metric_moves) if body.metric_moves is not None else None
        return run(
            caller,
            "catalog_split",
            caller_json(caller, approval),
            kind,
            slug,
            body.into,
            body.note,
            moves,
        )

    @app.post(
        "/v1/catalog/concepts/{kind}/{slug}/retire",
        response_model=Retired,
        tags=["catalog"],
        responses=ERRORS,
    )
    def retire_concept(
        caller: Caller,
        kind: Kind,
        slug: SlugPath,
        body: RetireBody,
        approval: Approval = None,
    ) -> Any:
        scoped(caller, "catalog.concept:admin", read=False)
        moves = Jsonb(body.metric_moves) if body.metric_moves is not None else None
        return run(
            caller,
            "catalog_retire",
            caller_json(caller, approval),
            kind,
            slug,
            body.note,
            moves,
        )

    @app.get(
        "/v1/catalog/metrics",
        response_model=MetricList,
        tags=["catalog"],
        responses=ERRORS,
    )
    def list_metrics(
        caller: Caller,
        domain: Annotated[str | None, Query(pattern=SLUG)] = None,
        kind: Literal["leading", "lagging", "guardrail", "descriptive"] | None = None,
    ) -> dict[str, Any]:
        scoped(caller, "catalog.metric-definition:read", read=True)
        return {
            "items": run(
                caller, "catalog_list_metrics", caller_json(caller), domain, kind
            )
        }

    KeyPath = Annotated[str, Path_(pattern=KEY, max_length=80)]

    @app.get(
        "/v1/catalog/metrics/{key}",
        response_model=Metric,
        tags=["catalog"],
        responses=ERRORS,
    )
    def get_metric(caller: Caller, key: KeyPath) -> Any:
        scoped(caller, "catalog.metric-definition:read", read=True)
        return run(caller, "catalog_get_metric", caller_json(caller), key)

    @app.put(
        "/v1/catalog/metrics/{key}",
        response_model=Metric,
        tags=["catalog"],
        responses=ERRORS,
    )
    def put_metric(
        caller: Caller, key: KeyPath, body: MetricPut, approval: Approval = None
    ) -> Any:
        scoped(caller, "catalog.metric-definition:write", read=False)
        definition = body.model_dump(exclude={"if_version"}, exclude_none=False)
        if definition["classification"] is None:
            del definition[
                "classification"
            ]  # the stored or register classification applies
        return run(
            caller,
            "catalog_put_metric",
            caller_json(caller, approval),
            key,
            Jsonb(definition),
            body.if_version,
        )

    @app.get(
        "/v1/catalog/review", response_model=Review, tags=["catalog"], responses=ERRORS
    )
    def review(caller: Caller) -> Any:
        scoped(caller, "catalog.concept:read", read=True)
        return run(caller, "catalog_review", caller_json(caller))

    @app.get(
        "/v1/catalog/versions",
        response_model=VersionList,
        tags=["catalog"],
        responses=ERRORS,
    )
    def versions(caller: Caller) -> dict[str, Any]:
        scoped(caller, "catalog.concept:read", read=True)
        return {"items": run(caller, "catalog_versions", caller_json(caller))}
