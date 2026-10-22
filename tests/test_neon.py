"""Neon provisioning: idempotent API calls, role attributes, and the secrets file."""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from empire_platform import neon
from empire_platform.devdb import HostedCluster


class FakeNeon(BaseHTTPRequestHandler):
    state: dict = {}  # noqa: RUF012 - shared fake-server state, reset by each test

    def log_message(self, *args):  # quiet
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def handle_any(self):
        s = self.state
        s["calls"].append((self.command, self.path.split("?")[0]))
        assert self.headers["Authorization"] == "Bearer test-key"
        path = urllib.parse.urlsplit(self.path).path
        if s.get("lock_once") and self.command == "POST":
            s["lock_once"] = False
            return self.reply(423, {"message": "locked"})
        op = {"operations": [{"id": "op-1", "action": "apply_config"}]}
        if path == "/projects" and self.command == "GET":
            return self.reply(200, {"projects": [{"id": "p-1", "name": "empire-platform"}] if s["project"] else []})
        if path == "/projects" and self.command == "POST":
            s["project"] = True
            assert self.body()["project"]["pg_version"] == 16
            return self.reply(201, {"project": {"id": "p-1"}, **op})
        if path == "/projects/p-1/operations/op-1":
            s["polls"] += 1
            return self.reply(200, {"operation": {"status": "finished" if s["polls"] % 2 == 0 else "running"}})
        if path == "/projects/p-1/branches":
            return self.reply(200, {"branches": [{"id": "br-x", "default": False}, {"id": "br-1", "default": True}]})
        if path == "/projects/p-1/endpoints":
            return self.reply(200, {"endpoints": [{"branch_id": "br-1", "type": "read_write", "host": "ep-1.neon.test"}]})
        base = "/projects/p-1/branches/br-1"
        if path == f"{base}/roles" and self.command == "GET":
            return self.reply(200, {"roles": [{"name": n} for n in s["roles"]]})
        if path == f"{base}/roles" and self.command == "POST":
            s["roles"].append(self.body()["role"]["name"])
            return self.reply(201, {"role": {"name": "empire_migrator", "password": "pw"}, **op})  # pragma: allowlist secret (synthetic test value)
        if path == f"{base}/roles/empire_migrator/reveal_password":
            return self.reply(200, {"password": "pw/with?odd&chars"})  # pragma: allowlist secret (synthetic test value)
        if path == f"{base}/databases" and self.command == "GET":
            return self.reply(200, {"databases": [{"name": n} for n in s["databases"]]})
        if path == f"{base}/databases" and self.command == "POST":
            body = self.body()["database"]
            assert body == {"name": "empire", "owner_name": "empire_migrator"}
            s["databases"].append(body["name"])
            return self.reply(201, {"database": body, **op})
        return self.reply(404, {"message": f"unexpected {path}"})

    do_GET = do_POST = handle_any


@pytest.fixture
def fake_neon():
    FakeNeon.state = {"project": False, "roles": ["neondb_owner"], "databases": ["neondb"], "calls": [], "polls": 0,
                      "lock_once": True}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNeon)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield neon.Neon("test-key", base=f"http://127.0.0.1:{server.server_port}", timeout=5), FakeNeon.state
    server.shutdown()


def test_provisioning_api_calls_are_idempotent(fake_neon):
    client, state = fake_neon
    first = neon.ensure_project(client, "empire-platform", "aws-ap-southeast-1")
    password = neon.ensure_migrator(client, *first[:2])
    assert first == ("p-1", "br-1", "ep-1.neon.test")
    assert password == "pw/with?odd&chars"
    creates = [c for c in state["calls"] if c[0] == "POST"]
    assert len(creates) == 4  # project (after one 423 retry), migrator role, database
    state["calls"].clear()
    assert neon.ensure_project(client, "empire-platform", "aws-ap-southeast-1") == first
    neon.ensure_migrator(client, "p-1", "br-1")
    assert not [c for c in state["calls"] if c[0] == "POST"]
    built = neon.url("ep-1.neon.test", "empire_migrator", password)
    assert urllib.parse.unquote(urllib.parse.urlsplit(built).password) == password
    assert built.endswith("/empire?sslmode=require&channel_binding=require")


def test_runtime_roles_are_least_privilege_and_keep_their_passwords(cluster):
    with cluster.connect("postgres", autocommit=True) as conn:
        conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'neon_superuser')"
                     " THEN CREATE ROLE neon_superuser NOLOGIN; END IF; END $$")
    owner = f"postgresql:///empire?host={cluster.socket_dir}&port=5432&user=postgres"
    first = neon.ensure_runtime_roles(owner, {})
    assert set(first) == set(neon.RUNTIME_ROLES)
    assert len(set(first.values())) == len(first)
    kept = neon.ensure_runtime_roles(owner, {"tasks_api": neon.url("h", "tasks_api", first["tasks_api"])})
    assert kept["tasks_api"] == first["tasks_api"]
    socket = {role: f"postgresql:///empire?host={cluster.socket_dir}&port=5432&user={role}"
              for role in (*neon.RUNTIME_ROLES, "empire_migrator")}
    assert neon.verify(HostedCluster(socket)) == []


def test_secrets_file_is_owner_only_and_parsed(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "db.env"
    neon.write_secrets(path, {"tasks_api": "postgresql://a?sslmode=require", "empire_migrator": "postgresql://b?sslmode=require"})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    monkeypatch.delenv("EMPIRE_DB_URL_TASKS_API", raising=False)
    cluster = HostedCluster.from_env(path)
    assert cluster.conninfo("tasks_api") == "postgresql://a?sslmode=require"
    assert cluster.conninfo("postgres") == "postgresql://b?sslmode=require"  # no superuser on Neon: the migrator stands in
    monkeypatch.setenv("EMPIRE_DB_URL_TASKS_API", "postgresql://from-env?sslmode=require")
    assert HostedCluster.from_env(path).conninfo("tasks_api") == "postgresql://from-env?sslmode=require"
    path.chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        HostedCluster.from_env(path)


def test_default_cluster_switches_on_mode(monkeypatch, tmp_path):
    from empire_platform.devdb import Cluster, default_cluster

    monkeypatch.delenv("EMPIRE_DB_MODE", raising=False)
    assert isinstance(default_cluster(), Cluster)
    monkeypatch.setenv("EMPIRE_DB_MODE", "hosted")
    monkeypatch.setattr("empire_platform.devdb.HOSTED_SECRETS", tmp_path / "none.env")
    assert isinstance(default_cluster(), HostedCluster)


def test_hosted_urls_must_use_verifying_tls():
    good = {r: f"postgresql://{r}:pw@h/empire?sslmode={mode}" for r, mode in  # pragma: allowlist secret (synthetic test value)
            (("a", "require"), ("b", "verify-ca"), ("c", "verify-full"))}
    cluster = HostedCluster({**good, "d": "postgresql://d:SECRET@h/empire?sslmode=prefer",  # pragma: allowlist secret (synthetic test value)
                             "e": "postgresql://e:SECRET@h/empire", "f": "postgresql://f:SECRET@h/empire?sslmode=disable"})  # pragma: allowlist secret (synthetic test value)
    assert [cluster.conninfo(r) for r in "abc"] == list(good.values())
    for role in "def":
        with pytest.raises(SystemExit) as error:
            cluster.conninfo(role)
        assert role in str(error.value)
        assert "SECRET" not in str(error.value)
        assert "h/empire" not in str(error.value)


def test_neon_api_never_follows_redirects():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for server in (target, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(neon.NeonError, match="302"):
            neon.Neon("test-key", base=f"http://127.0.0.1:{origin.server_port}", timeout=5).call("GET", "/projects")
    finally:
        origin.shutdown()
        target.shutdown()
    assert seen == []


def test_secrets_write_is_atomic_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "db.env"
    neon.write_secrets(path, {"tasks_api": "postgresql://a"})
    neon.write_secrets(path, {"tasks_api": "postgresql://b"})
    assert path.read_text().endswith("EMPIRE_DB_URL_TASKS_API=postgresql://b\n")
    assert [p.name for p in path.parent.iterdir()] == ["db.env"]
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        neon.write_secrets(path, {"tasks_api": "postgresql://c"})
    assert [p.name for p in path.parent.iterdir()] == ["db.env"]
    assert "://b" in path.read_text()


def test_verify_flags_runtime_role_memberships(cluster):
    socket = {role: f"postgresql:///empire?host={cluster.socket_dir}&port=5432&user={role}"
              for role in (*neon.RUNTIME_ROLES, "empire_migrator")}
    with cluster.connect("postgres", autocommit=True) as conn:
        conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'neon_superuser')"
                     " THEN CREATE ROLE neon_superuser NOLOGIN; END IF; END $$")
        conn.execute("DROP ROLE IF EXISTS probe_group")
        conn.execute("CREATE ROLE probe_group NOLOGIN")
        conn.execute("GRANT probe_group TO tasks_api")
    try:
        assert any("tasks_api" not in p and "members of 1" in p for p in neon.verify(HostedCluster(socket)))
    finally:
        with cluster.connect("postgres", autocommit=True) as conn:
            conn.execute("REVOKE probe_group FROM tasks_api")
            conn.execute("DROP ROLE probe_group")
