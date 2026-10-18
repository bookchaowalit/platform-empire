"""Local PostgreSQL for development and tests, plus the migration runner.

The cluster lives under ``.runtime/pgdata`` and listens only on a Unix socket,
so nothing is exposed on the network and no credentials are needed. Hosted
databases (Neon) are out of scope here and need separate owner approval.

    empire-db start | stop | reset | status | migrate | psql [--role ROLE]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]


def contracts_dir() -> Path:
    """The contracts folder: EMPIRE_CONTRACTS_DIR, the source checkout, or the image's /app.

    ``pip install .`` puts this package in site-packages, where REPO_ROOT no longer holds
    contracts/; the Docker image copies them to /app/contracts (its WORKDIR)."""
    candidates = [
        os.environ.get("EMPIRE_CONTRACTS_DIR"),
        REPO_ROOT / "contracts",
        Path.cwd() / "contracts",
        Path("/app/contracts"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "automations" / "v1").is_dir():
            return Path(candidate)
    raise FileNotFoundError(
        "contracts/automations/v1 not found; set EMPIRE_CONTRACTS_DIR"
    )


BOOTSTRAP = REPO_ROOT / "db" / "bootstrap" / "roles.sql"
MIGRATION_DIRS = [
    REPO_ROOT / "services" / "platform" / "migrations",
    REPO_ROOT / "services" / "tasks" / "migrations",
    REPO_ROOT / "services" / "graph" / "migrations",
    REPO_ROOT / "services" / "monitor" / "migrations",
]
DATABASE = "empire"
SUPERUSER = "postgres"
HOSTED_SSLMODES = ("require", "verify-ca", "verify-full")
PORT = 5432  # only used to name the Unix socket file; TCP is disabled


def write_private_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically with mode 0600 (temp file in the same directory)."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def pg_bin_dir() -> Path:
    """PostgreSQL binaries: $EMPIRE_PG_BIN, the pgserver wheel, or PATH."""
    override = os.environ.get("EMPIRE_PG_BIN")
    if override:
        return Path(override)
    try:
        import pgserver  # type: ignore[import-untyped]

        bundled = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (bundled / "pg_ctl").exists():
            return bundled
    except ImportError:
        pass
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    raise SystemExit(
        "PostgreSQL binaries not found; install the dev extra or set EMPIRE_PG_BIN"
    )


@dataclass
class Cluster:
    data_dir: Path
    socket_dir: Path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(pg_bin_dir() / args[0]), *args[1:]],
            check=True,
            capture_output=True,
            text=True,
        )

    def init(self) -> None:
        self.data_dir.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            "initdb",
            "-D",
            str(self.data_dir),
            "-U",
            SUPERUSER,
            "--auth=trust",
            "--encoding=UTF8",
            "--locale=C",
        )

    def start(self) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = f"-c listen_addresses='' -k {self.socket_dir} -p {PORT}"
        self._run(
            "pg_ctl",
            "-D",
            str(self.data_dir),
            "-o",
            options,
            "-w",
            "-l",
            str(self.data_dir / "server.log"),
            "start",
        )

    def stop(self) -> None:
        if (self.data_dir / "postmaster.pid").exists():
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "-w", "stop")

    def running(self) -> bool:
        return (self.data_dir / "postmaster.pid").exists()

    def conninfo(self, role: str, dbname: str = DATABASE) -> str:
        return f"host={self.socket_dir} port={PORT} user={role} dbname={dbname}"

    def connect(
        self, role: str, dbname: str = DATABASE, **kwargs
    ) -> psycopg.Connection:
        return psycopg.connect(self.conninfo(role, dbname), **kwargs)


HOSTED_SECRETS = Path(
    os.environ.get(
        "EMPIRE_DB_SECRETS", Path.home() / ".config" / "empire-platform" / "db.env"
    )
)


@dataclass
class HostedCluster:
    """A hosted database (Neon): one connection URL per role, never in the repo.

    URLs come from ``EMPIRE_DB_URL_<ROLE>`` in the environment (Railway
    variables) or from the owner-only file ``~/.config/empire-platform/db.env``
    (local operator commands). The database name is part of each URL; there is
    no superuser, so backup and restore use the migrator role.
    """

    urls: dict[str, str]

    @classmethod
    def from_env(cls, secrets: Path = HOSTED_SECRETS) -> HostedCluster:
        urls: dict[str, str] = {}
        if secrets.is_file():
            if secrets.stat().st_mode & 0o077:
                raise SystemExit(
                    f"{secrets} must be readable by its owner only (chmod 600)"
                )
            for line in secrets.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("EMPIRE_DB_URL_"):
                    urls[key.removeprefix("EMPIRE_DB_URL_").lower()] = value.strip()
        for key, value in os.environ.items():
            if key.startswith("EMPIRE_DB_URL_") and value:
                urls[key.removeprefix("EMPIRE_DB_URL_").lower()] = value
        return cls(urls)

    def conninfo(self, role: str, dbname: str = DATABASE) -> str:
        role = "empire_migrator" if role == SUPERUSER else role
        if role not in self.urls:
            raise SystemExit(
                f"no connection URL for role {role}: set EMPIRE_DB_URL_{role.upper()}"
            )
        url = self.urls[role]
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        # Unix-socket URLs (host=/path) are local and have no TLS; everything else must verify TLS.
        local = any(host.startswith("/") for host in query.get("host", []))
        if not local and query.get("sslmode", [""])[-1] not in HOSTED_SSLMODES:
            raise SystemExit(
                f"connection URL for role {role} needs sslmode=require, verify-ca or verify-full"
            )
        return url

    def connect(
        self, role: str, dbname: str = DATABASE, **kwargs
    ) -> psycopg.Connection:
        # Neon's pooler cannot keep prepared statements; direct endpoints are used.
        return psycopg.connect(self.conninfo(role, dbname), **kwargs)

    def running(self) -> bool:
        return True


def default_cluster() -> Cluster | HostedCluster:
    """The local development cluster, or the hosted one when EMPIRE_DB_MODE=hosted."""
    if os.environ.get("EMPIRE_DB_MODE") == "hosted":
        return HostedCluster.from_env()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return Cluster(REPO_ROOT / ".runtime" / "pgdata", runtime / "empire-platform")


def bootstrap(cluster: Cluster) -> None:
    """Create roles and the database; safe to run more than once."""
    with cluster.connect(SUPERUSER, "postgres", autocommit=True) as conn:
        conn.execute(BOOTSTRAP.read_text(encoding="utf-8"))
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
        ).fetchone()
        if not exists:
            conn.execute(f"CREATE DATABASE {DATABASE} OWNER empire_migrator")
    with cluster.connect(SUPERUSER, DATABASE, autocommit=True) as conn:
        conn.execute(f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC")
        conn.execute(
            f"GRANT CONNECT ON DATABASE {DATABASE} TO platform_api, tasks_api, tasks_publisher, graph_api,"
            " platform_monitor, identity_api"
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def migration_files() -> list[Path]:
    files = [
        path for directory in MIGRATION_DIRS for path in sorted(directory.glob("*.sql"))
    ]
    names = [path.name for path in files]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate migration file names")
    return files


def migrate(cluster: Cluster, dbname: str = DATABASE) -> list[str]:
    """Apply pending migrations as empire_migrator; refuse edited ones."""
    applied: list[str] = []
    with cluster.connect("empire_migrator", dbname) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = dict(
            conn.execute("SELECT name, sha256 FROM public.schema_migrations").fetchall()
        )
        for path in migration_files():
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise SystemExit(f"applied migration was edited: {path.name}")
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO public.schema_migrations (name, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-db", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "command", choices=["start", "stop", "reset", "status", "migrate", "psql"]
    )
    parser.add_argument("--role", default="platform_api")
    args = parser.parse_args(argv)
    cluster = default_cluster()

    if args.command == "reset":
        cluster.stop()
        shutil.rmtree(cluster.data_dir, ignore_errors=True)
    if args.command in {"start", "reset"}:
        if not (cluster.data_dir / "PG_VERSION").exists():
            cluster.init()
        if not cluster.running():
            cluster.start()
        bootstrap(cluster)
        for name in migrate(cluster):
            print(f"applied {name}")
        print(f"ready: {cluster.conninfo('platform_api')}")
    elif args.command == "stop":
        cluster.stop()
        print("stopped")
    elif args.command == "status":
        print(f"{'running' if cluster.running() else 'stopped'}: {cluster.data_dir}")
    elif args.command == "migrate":
        applied = migrate(cluster)
        print("\n".join(f"applied {name}" for name in applied) or "up to date")
    elif args.command == "psql":
        os.execv(str(pg_bin_dir() / "psql"), ["psql", cluster.conninfo(args.role)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
