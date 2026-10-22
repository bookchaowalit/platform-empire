"""Synthetic local restore drill; never resolve the development or hosted database."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

import psycopg
import pytest
from conftest import use_workspace
from fastapi.testclient import TestClient
from graph_service.consumer import GraphConsumer
from graph_world import ISSUER, apps, bearer, gkeys, gtoken, gworld, run_publisher  # noqa: F401
from psycopg import sql
from psycopg.rows import dict_row
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier
from tasks_api.publisher import publish

from empire_platform.devdb import Cluster, pg_bin_dir


def restore_copy(cluster, archive, dbname):
    """Only the test fixture's private Unix socket; no URL or default_cluster()."""
    root = cluster.data_dir.parent.resolve() if type(cluster) is Cluster else None
    if (
        root is None
        or not root.name.startswith("ep-")
        or cluster.data_dir.resolve() != root / "data"
        or cluster.socket_dir.resolve() != root / "sock"
    ):
        raise ValueError("recovery requires an isolated local test cluster")
    if not re.fullmatch(r"recovery_[a-f0-9]{32}", dbname):
        raise ValueError("recovery requires a unique test database name")
    archive.parent.chmod(0o700)
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    env = {
        "PGHOST": str(cluster.socket_dir),
        "PGPORT": "5432",
        "PGUSER": "postgres",
        "PGDATABASE": "empire",
        "PGCONNECT_TIMEOUT": "5",
    }

    def run(tool, *args):
        result = subprocess.run(
            [str(pg_bin_dir() / tool), *args],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,  # returncode is checked below
        )
        if result.returncode:
            category = next(
                (
                    kind
                    for kind in (
                        "already exists",
                        "permission denied",
                        "row-level security",
                        "violates",
                        "does not exist",
                        "unsupported version",
                    )
                    if kind.encode() in result.stderr
                ),
                "other",
            )
            raise RuntimeError(
                f"{tool} failed (exit {result.returncode}, category={category})"
            )

    run("pg_dump", "--format=custom", "--file", str(archive))
    with cluster.connect("postgres", "postgres", autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER empire_migrator").format(
                sql.Identifier(dbname)
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(dbname)
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO platform_api, tasks_api, tasks_publisher, "
                "graph_api, platform_monitor"
            ).format(sql.Identifier(dbname))
        )
    env["PGDATABASE"] = dbname
    try:
        run("pg_restore", "--exit-on-error", "--dbname", dbname, str(archive))
    except Exception:
        with cluster.connect("postgres", "postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
        raise


def snapshot(cluster, dbname="empire"):
    """Compare every restored row and migration checksum without printing payloads."""
    with cluster.connect("postgres", dbname) as conn:
        tables = conn.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind='r' AND (n.nspname IN ('platform','tasks','graph') "
            "OR (n.nspname='public' AND c.relname='schema_migrations')) ORDER BY 1,2"
        ).fetchall()
        return {
            (schema, table): conn.execute(
                sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY 1").format(
                    sql.Identifier(schema, table)
                )
            ).fetchall()
            for schema, table in tables
        }


def test_synthetic_dump_restore_preserves_data_security_and_delivery(
    cluster,
    apps,
    gworld,
    gtoken,
    tmp_path,
    record_property,
    request,
):
    source, _ = apps
    owner = bearer(gtoken(gworld.owner, gworld.ws))
    created = source.post(
        "/v1/tasks", headers=owner, json={"title": "Recovery synthetic marker"}
    )
    assert created.status_code == 201
    task = created.json()
    linked = source.post(
        f"/v1/tasks/{task['id']}/links",
        headers=owner,
        json={
            "relation": "supports-goal",
            "target_type": "goal",
            "target_ref": gworld.goal,
        },
    )
    assert linked.status_code == 201
    assert run_publisher(cluster).failed == 0
    updated = source.patch(
        f"/v1/tasks/{task['id']}",
        headers={**owner, "If-Match": str(task["version"])},
        json={"priority": 2},
    )
    assert updated.status_code == 200
    before = snapshot(cluster)
    dbname = "recovery_" + uuid.uuid4().hex
    archive = tmp_path / "synthetic.dump"
    started = time.monotonic()
    restore_copy(cluster, archive, dbname)
    restored_seconds = time.monotonic() - started
    try:
        assert archive.stat().st_mode & 0o777 == 0o600
        assert snapshot(cluster, dbname) == before
        with cluster.connect("postgres", dbname) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname IN ('platform','tasks') AND c.relkind='r' "
                    "AND c.relname <> 'settings' AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM pg_roles WHERE rolname IN ('platform_api','tasks_api','graph_api') "
                    "AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole)"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM tasks.outbox o LEFT JOIN platform.executions e ON e.id=o.execution_id "
                    "AND e.workspace_id=o.workspace_id WHERE e.id IS NULL"
                ).fetchone()[0]
                == 0
            )
        public_pem = request.getfixturevalue("gkeys")[1]
        recovered = TestClient(
            create_app(
                lambda: cluster.connect("tasks_api", dbname),
                TokenVerifier(public_pem, ISSUER, "tasks-api"),
            )
        )
        restored_read = recovered.get(f"/v1/tasks/{task['id']}", headers=owner)
        assert restored_read.status_code == 200
        assert restored_read.json()["priority"] == 2
        outsider = bearer(gtoken(gworld.outsider, gworld.beta))
        outsider_read = recovered.get(f"/v1/tasks/{task['id']}", headers=outsider)
        assert outsider_read.status_code == 404
        reader = bearer(
            gtoken(
                gworld.coach, gworld.ws, delegation=gworld.coach_read, obo=gworld.owner
            )
        )
        delegated_read = recovered.get(f"/v1/tasks/{task['id']}", headers=reader)
        assert delegated_read.status_code == 200
        denied_write = recovered.post(
            "/v1/tasks", headers=reader, json={"title": "Denied"}
        )
        assert denied_write.status_code == 403
        with cluster.connect("tasks_api", dbname) as conn:
            use_workspace(conn, gworld.beta)
            assert (
                conn.execute(
                    "SELECT count(*) FROM tasks.tasks WHERE id=%s", (task["id"],)
                ).fetchone()[0]
                == 0
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO tasks.tasks(workspace_id,title) VALUES (%s,'Denied')",
                        (gworld.ws,),
                    )
            use_workspace(conn, None)
            assert conn.execute("SELECT count(*) FROM tasks.tasks").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("ALTER TABLE tasks.tasks DISABLE ROW LEVEL SECURITY")
        with cluster.connect("platform_api", dbname) as conn:
            use_workspace(conn, gworld.ws)
            conn.execute(
                "UPDATE platform.delegations SET revoked_at=now() WHERE id=%s",
                (gworld.coach_read,),
            )
        revoked_read = recovered.get(f"/v1/tasks/{task['id']}", headers=reader)
        assert revoked_read.status_code == 404
        with cluster.connect("platform_api", dbname) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM platform.audit_events")
        consumer = GraphConsumer(lambda: cluster.connect("graph_api", dbname))
        report = publish(
            lambda: cluster.connect("tasks_publisher", dbname), consumer, batch=1000
        )
        assert report.failed == 0
        assert report.delivered >= 1
        with cluster.connect("postgres", dbname, row_factory=dict_row) as conn:
            event = conn.execute(
                "SELECT * FROM tasks.outbox WHERE subject=%s ORDER BY occurred_at DESC LIMIT 1",
                (task["urn"],),
            ).fetchone()
            event = json.loads(json.dumps(event, default=str))
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM tasks.outbox WHERE published_at IS NULL"
                ).fetchone()["n"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT version FROM graph.nodes WHERE urn=%s", (task["urn"],)
                ).fetchone()["version"]
                == 2
            )
        graph_before = snapshot(cluster, dbname)[("graph", "inbox")]
        consumer(event)
        consumer(event)
        assert snapshot(cluster, dbname)[("graph", "inbox")] == graph_before
        assert snapshot(cluster) == before, (
            "restore validation must not mutate its source"
        )
        metrics = {
            "scope": "local_synthetic",
            "restore_seconds": round(restored_seconds, 3),
            "restore_and_acceptance_seconds": round(time.monotonic() - started, 3),
            "dump_bytes": archive.stat().st_size,
            "tables_compared": len(before),
            "published_after_restore": report.delivered,
            "snapshot_data_loss_rows": 0,
        }
        record_property("recovery_rehearsal", json.dumps(metrics))
        print(json.dumps(metrics, sort_keys=True))
    finally:
        with cluster.connect("postgres", "postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))


def test_recovery_rejects_non_fixture_endpoint(tmp_path):
    cluster = Cluster(tmp_path / "data", tmp_path / "remote")
    with pytest.raises(ValueError, match="isolated local"):
        restore_copy(cluster, tmp_path / "blocked.dump", "recovery_" + uuid.uuid4().hex)
    assert not (tmp_path / "blocked.dump").exists()


def test_recovery_refuses_existing_archive(cluster, tmp_path):
    archive = tmp_path / "existing.dump"
    archive.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        restore_copy(cluster, archive, "recovery_" + uuid.uuid4().hex)
    assert archive.read_bytes() == b"preserve"


def test_recovery_refuses_existing_database(cluster, tmp_path):
    dbname = "recovery_" + uuid.uuid4().hex
    with cluster.connect("postgres", "postgres", autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    try:
        with cluster.connect("postgres", dbname) as conn:
            conn.execute("CREATE TABLE sentinel(value int)")
            conn.execute("INSERT INTO sentinel VALUES (42)")
        with pytest.raises(psycopg.errors.DuplicateDatabase):
            restore_copy(cluster, tmp_path / "safe.dump", dbname)
        with cluster.connect("postgres", dbname) as conn:
            assert conn.execute("SELECT value FROM sentinel").fetchone() == (42,)
    finally:
        with cluster.connect("postgres", "postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
