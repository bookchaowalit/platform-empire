"""Provision automation identities and their delegations from the registry.

Each automation in contracts/automations/v1/automations.json becomes an
``automation`` identity in the workspace with a delegation from the
workspace owner carrying only the registry scopes. Provisioning is
idempotent: an unchanged, unexpired delegation is kept; a changed or
expiring one is replaced and the old one revoked.

    empire-automations keys --dir DIR
    empire-automations provision --workspace SLUG [--tokens DIR] [--key-dir DIR] [--issuer ISS] [--ttl-days N]
    empire-automations owner-token --workspace SLUG --name NAME --tokens DIR [--key-dir DIR] [--issuer ISS]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row

from empire_platform.devdb import (
    REPO_ROOT,
    Cluster,
    default_cluster,
    write_private_file,
)

REGISTRY = REPO_ROOT / "contracts" / "automations" / "v1" / "automations.json"
RENEW_BEFORE = timedelta(
    days=10
)  # = rotate_within_days: a due token always renews a closing delegation
OWNER_TOKEN_MAX_TTL = 3600  # an owner token has no delegation, so nothing can revoke it


@dataclass(frozen=True)
class Provisioned:
    automation: str
    identity: uuid.UUID
    delegation: uuid.UUID
    owner: uuid.UUID
    scopes: tuple[str, ...]
    renewed: bool
    expires_at: datetime | None = (
        None  # the delegation's expiry; a token must not outlive it
    )


def load_registry(path: Path = REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def provision(
    cluster: Cluster,
    workspace_id: uuid.UUID,
    registry: dict | None = None,
    only: set[str] | None = None,
) -> list[Provisioned]:
    """Provision every registry automation, or just the ids in ``only``."""
    registry = registry or load_registry()
    days = int(registry["delegation_days"])
    if only is not None:
        unknown = only - {a["id"] for a in registry["automations"]}
        if unknown:
            raise KeyError(f"unknown automation(s): {', '.join(sorted(unknown))}")
    results: list[Provisioned] = []
    with cluster.connect("platform_api") as conn:
        conn.row_factory = dict_row
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),)
        )
        owner = conn.execute(
            "SELECT owner_identity_id FROM platform.workspaces WHERE id = %s",
            (workspace_id,),
        ).fetchone()["owner_identity_id"]
        for automation in registry["automations"]:
            if only is not None and automation["id"] not in only:
                continue
            subject = f"automation:{workspace_id}:{automation['id']}"
            scopes = tuple(sorted(automation["scopes"]))
            row = conn.execute(
                "SELECT id FROM platform.identities WHERE external_subject = %s",
                (subject,),
            ).fetchone()
            if row is None:
                identity = conn.execute("SELECT platform.uuid_v7() AS id").fetchone()[
                    "id"
                ]
                conn.execute(
                    "INSERT INTO platform.identities (id, kind, display_name, external_subject)"
                    " VALUES (%s, 'automation', %s, %s)",
                    (identity, automation["id"], subject),
                )
                conn.execute(
                    "INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (%s, %s)",
                    (workspace_id, identity),
                )
            else:
                identity = row["id"]
            current = conn.execute(
                "SELECT id, scopes::text[] AS scopes, expires_at FROM platform.delegations"
                " WHERE grantee_identity_id = %s AND grantor_identity_id = %s AND revoked_at IS NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (identity, owner),
            ).fetchone()
            now = datetime.now(UTC)
            if (
                current
                and tuple(sorted(current["scopes"])) == scopes
                and current["expires_at"] - now > RENEW_BEFORE
            ):
                results.append(
                    Provisioned(
                        automation["id"],
                        identity,
                        current["id"],
                        owner,
                        scopes,
                        False,
                        current["expires_at"],
                    )
                )
                continue
            conn.execute(
                "UPDATE platform.delegations SET revoked_at = now() WHERE grantee_identity_id = %s"
                " AND revoked_at IS NULL",
                (identity,),
            )
            delegation = conn.execute("SELECT platform.uuid_v7() AS id").fetchone()[
                "id"
            ]
            expires_at = conn.execute(
                "INSERT INTO platform.delegations (id, workspace_id, grantor_identity_id, grantee_identity_id, scopes,"
                " expires_at) VALUES (%s, %s, %s, %s, %s::platform.permission[], now() + make_interval(days => %s))"
                " RETURNING expires_at",
                (delegation, workspace_id, owner, identity, list(scopes), days),
            ).fetchone()["expires_at"]
            results.append(
                Provisioned(
                    automation["id"],
                    identity,
                    delegation,
                    owner,
                    scopes,
                    True,
                    expires_at,
                )
            )
        conn.commit()
    return results


def token_ttl(ttl: int, delegation_expires: datetime | None) -> int:
    """Never let a token outlive its delegation: a shorter token is rotated (and the
    delegation renewed) by the next run instead of failing once the delegation lapses."""
    if delegation_expires is None:
        return ttl
    remaining = round((delegation_expires - datetime.now(UTC)).total_seconds())
    return max(60, min(ttl, remaining))


def write_token(directory: Path, name: str, token: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{name}.jwt"
    write_private_file(path, token)
    return path


def _signing_key(key_dir: Path | None) -> str:
    from empire_platform.devtoken import key_pair

    if key_dir is None:
        return key_pair()[0]
    private = key_dir / "private.pem"
    if not private.is_file():
        raise SystemExit(
            f"no private key in {key_dir}; run: empire-automations keys --dir {key_dir}"
        )
    return private.read_text(encoding="utf-8")


def _add_key_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--key-dir",
        type=Path,
        help="directory with private.pem (default: the dev keys)",
    )
    parser.add_argument("--issuer", help="token issuer (default: the dev issuer)")


def main(argv: list[str] | None = None) -> int:
    from empire_platform.devtoken import ISSUER, generate_keys, mint

    parser = argparse.ArgumentParser(
        prog="empire-automations", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keys", help="generate a production RS256 key pair")
    keys.add_argument("--dir", type=Path, required=True)
    run = sub.add_parser("provision")
    run.add_argument("--workspace", required=True, help="workspace slug")
    run.add_argument(
        "--tokens", type=Path, help="write a token per automation into this directory"
    )
    run.add_argument(
        "--ttl-days", type=float, help="token lifetime in days (default: 1 hour)"
    )
    run.add_argument(
        "--only",
        action="append",
        metavar="AUTOMATION",
        help="provision and mint only this automation (repeatable); others are left untouched",
    )
    _add_key_options(run)
    owner = sub.add_parser(
        "owner-token",
        help="token for the workspace owner (no delegation), for the CLIs",
    )
    owner.add_argument("--workspace", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--tokens", type=Path, required=True)
    owner.add_argument(
        "--ttl-days",
        type=float,
        help="token lifetime in days (at most 1 hour = 0.0416)",
    )
    _add_key_options(owner)
    args = parser.parse_args(argv)

    if args.command == "keys":
        try:
            public = generate_keys(args.dir)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(public)
        return 0

    ttl = int(args.ttl_days * 86400) if args.ttl_days else 3600
    if args.command == "owner-token" and ttl > OWNER_TOKEN_MAX_TTL:
        print(
            "owner-token is limited to 1 hour; the CLIs use the delegated owner-cli token from provision",
            file=sys.stderr,
        )
        return 2
    issuer = args.issuer or ISSUER
    cluster = default_cluster()
    with cluster.connect("postgres") as conn:
        found = conn.execute(
            "SELECT id, owner_identity_id FROM platform.workspaces WHERE slug = %s",
            (args.workspace,),
        ).fetchone()
    if not found:
        print(f"unknown workspace {args.workspace}", file=sys.stderr)
        return 2
    private_pem = _signing_key(args.key_dir)
    if args.command == "owner-token":
        write_token(
            args.tokens,
            args.name,
            mint(private_pem, sub=found[1], ws=found[0], ttl=ttl, issuer=issuer),
        )
        print(f"{args.name:20} owner token written")
        return 0
    try:
        results = provision(
            cluster, found[0], only=set(args.only) if args.only else None
        )
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    if args.tokens:
        for item in results:
            write_token(
                args.tokens,
                item.automation,
                mint(
                    private_pem,
                    sub=item.identity,
                    ws=found[0],
                    delegation=item.delegation,
                    obo=item.owner,
                    ttl=token_ttl(ttl, item.expires_at),
                    issuer=issuer,
                ),
            )
    for item in results:
        print(
            f"{item.automation:20} {'renewed' if item.renewed else 'kept':8} {', '.join(item.scopes)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
