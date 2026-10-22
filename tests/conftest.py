"""Each test session gets its own throwaway PostgreSQL cluster."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from empire_platform.devdb import Cluster, bootstrap, migrate


@pytest.fixture(scope="session")
def cluster() -> Iterator[Cluster]:
    root = Path(
        tempfile.mkdtemp(prefix="ep-")
    )  # short path keeps the socket under 107 chars
    cluster = Cluster(root / "data", root / "sock")
    cluster.init()
    cluster.start()
    try:
        bootstrap(cluster)
        migrate(cluster)
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def api(cluster: Cluster) -> Iterator[psycopg.Connection]:
    """A runtime connection as platform_api; every test runs in its own transaction."""
    with cluster.connect("platform_api") as conn:
        yield conn
        conn.rollback()


def use_workspace(conn: psycopg.Connection, workspace_id: uuid.UUID | None) -> None:
    """Equivalent of SET LOCAL app.workspace_id for the current transaction."""
    conn.execute(
        "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id or ""),)
    )


def new_id(conn: psycopg.Connection) -> uuid.UUID:
    row = conn.execute("SELECT platform.uuid_v7()").fetchone()
    assert row is not None  # a SELECT without FROM always yields one row
    return row[0]


@dataclass
class Workspace:
    id: uuid.UUID
    owner: uuid.UUID
    owner_role: uuid.UUID
    members: dict[str, uuid.UUID] = field(default_factory=dict)


def create_workspace(
    conn: psycopg.Connection, slug: str, owner_permissions: list[str]
) -> Workspace:
    """Create a workspace, its human owner, and an owner role through RLS."""
    workspace_id, owner_id, role_id = new_id(conn), new_id(conn), new_id(conn)
    use_workspace(conn, workspace_id)
    conn.execute(
        "INSERT INTO platform.identities (id, kind, display_name) VALUES (%s, 'human', %s)",
        (owner_id, f"owner of {slug}"),
    )
    conn.execute(
        "INSERT INTO platform.workspaces (id, slug, name, owner_identity_id) VALUES (%s, %s, %s, %s)",
        (workspace_id, slug, slug.title(), owner_id),
    )
    conn.execute(
        "INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (%s, %s)",
        (workspace_id, owner_id),
    )
    conn.execute(
        "INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, 'owner')",
        (role_id, workspace_id),
    )
    for permission in owner_permissions:
        conn.execute(
            "INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
            (workspace_id, role_id, permission),
        )
    conn.execute(
        "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
        (workspace_id, owner_id, role_id),
    )
    return Workspace(workspace_id, owner_id, role_id)


def add_member(
    conn: psycopg.Connection, workspace: Workspace, name: str, kind: str
) -> uuid.UUID:
    identity_id = new_id(conn)
    use_workspace(conn, workspace.id)
    conn.execute(
        "INSERT INTO platform.identities (id, kind, display_name) VALUES (%s, %s, %s)",
        (identity_id, kind, name),
    )
    conn.execute(
        "INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (%s, %s)",
        (workspace.id, identity_id),
    )
    workspace.members[name] = identity_id
    return identity_id


def delegate(
    conn: psycopg.Connection,
    workspace: Workspace,
    grantor: uuid.UUID,
    grantee: uuid.UUID,
    scopes: list[str],
    expires_in: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    delegation_id = new_id(conn)
    use_workspace(conn, workspace.id)
    conn.execute(
        "INSERT INTO platform.delegations (id, workspace_id, grantor_identity_id, grantee_identity_id,"
        " scopes, expires_at) VALUES (%s, %s, %s, %s, %s::platform.permission[], %s)",
        (
            delegation_id,
            workspace.id,
            grantor,
            grantee,
            scopes,
            datetime.now(UTC) + expires_in,
        ),
    )
    return delegation_id


def has_permission(
    conn: psycopg.Connection,
    identity: uuid.UUID,
    permission: str,
    delegation: uuid.UUID | None = None,
) -> bool:
    row = conn.execute(
        "SELECT platform.has_permission(%s, %s, %s)", (identity, permission, delegation)
    ).fetchone()
    assert row is not None  # a SELECT without FROM always yields one row
    return row[0]
