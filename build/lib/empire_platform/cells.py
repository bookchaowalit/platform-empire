"""Owner tooling for cells: grant a cell's permissions, reconcile its executions.

A new cell (for example ``decisions``) brings its own permission names. The
owner role of a workspace gets them once, through ``grant``; automations
then receive narrower subsets from contracts/automations/v1 as usual.

    empire-cells grant --workspace SLUG --cell decisions [--dry-run]

Idempotent: permissions the role already has are left alone.

A cell with its own database commits locally and then finishes its platform
execution over HTTP; when that call is lost the execution stays ``running``.
``reconcile`` lists such executions (older than 15 minutes) and closes one,
after the owner has checked the cell's database
(docs/runbooks/cell-executions-and-tokens.md):

    empire-cells reconcile [--workspace owner] --cell decisions
    empire-cells reconcile [--workspace owner] --cell decisions --execution ID --fail
    empire-cells reconcile [--workspace owner] --cell decisions --execution ID --succeed --action ACTION

Closing goes through ``platform.finish_cell_execution`` with the authority the
execution was started with, so a success can only audit the bound operation.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, timedelta

from psycopg.rows import dict_row

from empire_platform.devdb import Cluster, default_cluster
from empire_platform.permissions import CELL_OWNERS


def grant(
    cluster: Cluster, workspace_id: uuid.UUID, cell: str, *, apply: bool = True
) -> list[str]:
    """Permissions added to the owner role (or that would be, without ``apply``)."""
    permissions = CELL_OWNERS[cell]
    with cluster.connect("platform_api") as conn:
        conn.row_factory = dict_row
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),)
        )
        role = conn.execute(
            "SELECT id FROM platform.roles WHERE workspace_id = %s AND name = 'owner'",
            (workspace_id,),
        ).fetchone()
        if role is None:
            raise SystemExit("workspace has no owner role")
        have = {
            r["permission"]
            for r in conn.execute(
                "SELECT permission FROM platform.role_permissions WHERE role_id = %s",
                (role["id"],),
            )
        }
        missing = [p for p in permissions if p not in have]
        for permission in missing:
            conn.execute(
                "INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
                (workspace_id, role["id"], permission),
            )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return missing


def _bound(cluster: Cluster, workspace_id: uuid.UUID):
    conn = cluster.connect("platform_api")
    conn.row_factory = dict_row
    conn.execute(
        "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),)
    )
    return conn


def stuck(
    cluster: Cluster,
    workspace_id: uuid.UUID,
    cell: str,
    min_age: timedelta = timedelta(minutes=15),
) -> list[dict]:
    """Executions of ``cell`` still running after ``min_age`` (never less than 15 minutes)."""
    with _bound(cluster, workspace_id) as conn:
        return conn.execute(
            "SELECT * FROM platform.stuck_cell_executions(%s, %s)", (cell, min_age)
        ).fetchall()


def reconcile(
    cluster: Cluster,
    workspace_id: uuid.UUID,
    cell: str,
    execution: uuid.UUID,
    status: str,
    action: str | None = None,
) -> str:
    """Close one stuck execution; returns the outcome of ``platform.reconcile_cell_execution``."""
    with _bound(cluster, workspace_id) as conn:
        outcome = conn.execute(
            "SELECT platform.reconcile_cell_execution(%s, %s, %s, %s) AS outcome",
            (execution, cell, status, action),
        ).fetchone()["outcome"]
        conn.commit()
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-cells", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("grant")
    run.add_argument("--workspace", required=True, help="workspace slug")
    run.add_argument("--cell", required=True, choices=sorted(CELL_OWNERS))
    run.add_argument("--dry-run", action="store_true")
    fix = sub.add_parser(
        "reconcile", help="list or close executions left running by a lost finish call"
    )
    fix.add_argument(
        "--workspace", default="owner", help="workspace slug (default: %(default)s)"
    )
    fix.add_argument("--cell", required=True, choices=sorted(CELL_OWNERS))
    fix.add_argument(
        "--execution", type=uuid.UUID, help="the execution to close (omit to list)"
    )
    outcome = fix.add_mutually_exclusive_group()
    outcome.add_argument(
        "--fail", action="store_true", help="the cell has no row for this execution"
    )
    outcome.add_argument(
        "--succeed", action="store_true", help="the cell committed; audit ACTION"
    )
    fix.add_argument(
        "--action",
        help="audit action the cell recorded, e.g. decisions.decision.updated",
    )
    args = parser.parse_args(argv)
    if args.command == "reconcile":
        if args.execution and not (args.fail or args.succeed):
            parser.error("--execution needs --fail or --succeed")
        if (args.fail or args.succeed) and not args.execution:
            parser.error("--fail and --succeed need --execution")
        if args.succeed != bool(args.action):
            parser.error("--action goes with --succeed, and --succeed needs --action")
    cluster = default_cluster()
    with cluster.connect("postgres") as conn:
        found = conn.execute(
            "SELECT id FROM platform.workspaces WHERE slug = %s", (args.workspace,)
        ).fetchone()
    if not found:
        print(f"unknown workspace {args.workspace}", file=sys.stderr)
        return 2
    if args.command == "reconcile":
        if not args.execution:
            rows = stuck(cluster, found[0], args.cell)
            for row in rows:
                started = row["started_at"].astimezone(UTC)
                print(
                    f"{row['execution_id']}  {started:%Y-%m-%d %H:%M}Z  {row['permission']}"
                    f"  {row['resource_type']}:{row['resource_id']}"
                )
            print(f"{args.cell}: {len(rows)} executions running over 15 min")
            return 0
        status = "succeeded" if args.succeed else "failed"
        result = reconcile(
            cluster, found[0], args.cell, args.execution, status, args.action
        )
        print(
            f"{args.execution}: {result}"
            + (f" ({status})" if result == "finished" else "")
        )
        return 0 if result == "finished" else 1
    added = grant(cluster, found[0], args.cell, apply=not args.dry_run)
    verb = "would add" if args.dry_run else "added"
    print(
        f"{args.cell}: {verb} {len(added)} owner permissions"
        + (f" ({', '.join(added)})" if added else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
