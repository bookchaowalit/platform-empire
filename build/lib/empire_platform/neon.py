"""Provision the hosted database on Neon and apply the migrations.

    NEON_API_KEY=... empire-neon provision [--project empire-platform] [--region aws-ap-southeast-1]
    empire-neon verify

``provision`` is idempotent: it reuses the project, branch, database, and roles
that already exist. It creates

- the migrator through the Neon API (it owns the ``empire`` database), and
- every runtime role through SQL as the migrator, with LOGIN, a generated
  password, NOSUPERUSER, NOBYPASSRLS, and NOINHERIT, so no runtime role is a
  member of ``neon_superuser``.

Connection URLs (direct endpoint, TLS required) are written to the owner-only
file ``~/.config/empire-platform/db.env``; nothing secret is printed. The
migrations then run as the migrator, and ``verify`` checks the role
attributes the security model depends on.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from empire_platform.devdb import (
    DATABASE,
    HOSTED_SECRETS,
    HostedCluster,
    migrate,
    write_private_file,
)

API = "https://console.neon.tech/api/v2"
MIGRATOR = "empire_migrator"
RUNTIME_ROLES = (
    "platform_api",
    "tasks_api",
    "tasks_publisher",
    "graph_api",
    "platform_monitor",
    "identity_api",
)


class NeonError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Bearer API key must not be forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Neon:
    def __init__(self, api_key: str, base: str = API, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(10):
            try:
                with _OPENER.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                if (
                    error.code == 423 and attempt < 9
                ):  # project locked by a running operation
                    time.sleep(2)
                    continue
                # Only the status and Neon's message; never the request (it may carry secrets).
                try:
                    message = json.loads(error.read()).get("message", "")
                except (ValueError, AttributeError):
                    message = ""
                raise NeonError(
                    f"{method} {path.split('?')[0]}: {error.code} {message}"[:300]
                ) from None
        raise NeonError(f"{method} {path}: still locked")

    def wait(
        self, project: str, payload: dict[str, Any], timeout: float = 120.0
    ) -> None:
        """Wait for the operations a call started."""
        deadline = time.monotonic() + timeout
        for operation in payload.get("operations", []):
            while True:
                status = self.call(
                    "GET", f"/projects/{project}/operations/{operation['id']}"
                )["operation"]["status"]
                if status == "finished":
                    break
                if (
                    status in ("failed", "error", "cancelled", "skipped")
                    or time.monotonic() > deadline
                ):
                    raise NeonError(
                        f"operation {operation.get('action')} ended as {status}"
                    )
                time.sleep(1)


def ensure_project(neon: Neon, name: str, region: str) -> tuple[str, str, str]:
    """(project id, default branch id, read-write endpoint host)."""
    listing = neon.call("GET", f"/projects?search={urllib.parse.quote(name)}")
    found = [p for p in listing.get("projects", []) if p["name"] == name]
    if found:
        project = found[0]["id"]
    else:
        created = neon.call(
            "POST",
            "/projects",
            {"project": {"name": name, "region_id": region, "pg_version": 16}},
        )
        project = created["project"]["id"]
        neon.wait(project, created)
    branches = neon.call("GET", f"/projects/{project}/branches")["branches"]
    branch = next(b for b in branches if b.get("default") or b.get("primary"))["id"]
    endpoints = neon.call("GET", f"/projects/{project}/endpoints")["endpoints"]
    host = next(
        e for e in endpoints if e["branch_id"] == branch and e["type"] == "read_write"
    )["host"]
    return project, branch, host


def ensure_migrator(neon: Neon, project: str, branch: str) -> str:
    """The migrator's password; created through the API so it can create roles."""
    base = f"/projects/{project}/branches/{branch}"
    names = {r["name"] for r in neon.call("GET", f"{base}/roles")["roles"]}
    if MIGRATOR not in names:
        neon.wait(
            project, neon.call("POST", f"{base}/roles", {"role": {"name": MIGRATOR}})
        )
    password = neon.call("GET", f"{base}/roles/{MIGRATOR}/reveal_password")["password"]
    databases = {d["name"] for d in neon.call("GET", f"{base}/databases")["databases"]}
    if DATABASE not in databases:
        neon.wait(
            project,
            neon.call(
                "POST",
                f"{base}/databases",
                {"database": {"name": DATABASE, "owner_name": MIGRATOR}},
            ),
        )
    return password


def url(host: str, role: str, password: str) -> str:
    return (
        f"postgresql://{urllib.parse.quote(role)}:{urllib.parse.quote(password, safe='')}@{host}/{DATABASE}"
        "?sslmode=require&channel_binding=require"
    )


def ensure_runtime_roles(migrator_url: str, existing: dict[str, str]) -> dict[str, str]:
    """Create or re-key each runtime role as the migrator; returns role -> password."""
    passwords: dict[str, str] = {}
    with psycopg.connect(migrator_url, autocommit=True) as conn:
        for role in RUNTIME_ROLES:
            known = existing.get(role)
            password = (
                urllib.parse.unquote(urllib.parse.urlsplit(known).password or "")
                if known
                else ""
            )
            password = password or secrets.token_urlsafe(32)
            present = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            verb = "ALTER" if present else "CREATE"
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- DDL is composed with psycopg sql.SQL/sql.Identifier/sql.Literal, which quote identifiers and literals
            conn.execute(
                sql.SQL(
                    "{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD {}"
                ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(password))
            )
            passwords[role] = password
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- DDL is composed with psycopg sql.SQL/sql.Identifier/sql.Literal, which quote identifiers and literals
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(DATABASE)
            )
        )
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- DDL is composed with psycopg sql.SQL/sql.Identifier/sql.Literal, which quote identifiers and literals
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE),
                sql.SQL(", ").join(map(sql.Identifier, RUNTIME_ROLES)),
            )
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    return passwords


def write_secrets(path: Path, urls: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    lines = ["# empire-platform hosted database; owner-only, never commit or print\n"]
    lines += [
        f"EMPIRE_DB_URL_{role.upper()}={value}\n"
        for role, value in sorted(urls.items())
    ]
    write_private_file(path, "".join(lines))


def verify(cluster: HostedCluster) -> list[str]:
    """Problems with the hosted roles; empty means the security model holds."""
    problems = []
    with cluster.connect(MIGRATOR) as conn:
        for role in RUNTIME_ROLES:
            row = conn.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit,"
                " pg_has_role(%s, 'neon_superuser', 'MEMBER') FROM pg_roles WHERE rolname = %s",
                (role, role),
            ).fetchone()
            if row is None:
                problems.append(f"{role}: missing")
            elif any(row):
                problems.append(
                    f"{role}: has elevated attributes or neon_superuser membership"
                )
        members = conn.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = ANY(%s)",
            (list(RUNTIME_ROLES),),
        ).fetchone()[0]
        if members:
            problems.append(f"runtime roles are members of {members} other roles")
        owned = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE tableowner = ANY(%s)",
            (list(RUNTIME_ROLES),),
        ).fetchone()[0]
        if owned:
            problems.append(f"runtime roles own {owned} tables")
    for role in RUNTIME_ROLES:
        with cluster.connect(role) as conn:
            conn.execute("SELECT 1")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-neon", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    provision = sub.add_parser("provision")
    provision.add_argument("--project", default="empire-platform")
    provision.add_argument("--region", default="aws-ap-southeast-1")
    sub.add_parser("verify")
    args = parser.parse_args(argv)

    if args.command == "provision":
        key = os.environ.get("NEON_API_KEY")
        if not key:
            print(
                "NEON_API_KEY is not set (inject it from the secret provider)",
                file=sys.stderr,
            )
            return 2
        neon = Neon(key)
        project, branch, host = ensure_project(neon, args.project, args.region)
        migrator_url = url(host, MIGRATOR, ensure_migrator(neon, project, branch))
        existing = HostedCluster.from_env().urls if HOSTED_SECRETS.is_file() else {}
        passwords = ensure_runtime_roles(migrator_url, existing)
        urls = {
            MIGRATOR: migrator_url,
            **{role: url(host, role, pw) for role, pw in passwords.items()},
        }
        write_secrets(HOSTED_SECRETS, urls)
        applied = migrate(HostedCluster(urls))
        print(
            f"project {project} ({args.region}); {len(applied)} migrations applied; URLs in {HOSTED_SECRETS}"
        )
    problems = verify(HostedCluster.from_env())
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    print("hosted roles verified" if not problems else "hosted roles NOT safe")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
