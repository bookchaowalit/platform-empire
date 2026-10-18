"""Cutover and rollback tooling for the tasks cell (local rehearsal).

Pieces, each tested:

- ``backup`` / ``restore``: pg_dump/pg_restore of the whole database, so a
  schema change or bad data load can be undone from a known point.
- ``set_writes``: the tasks write switch (the API answers 503 while off).
- ``freeze_sqlite`` / ``unfreeze_sqlite``: triggers that make the SQLite
  tasks tables refuse writes while PostgreSQL is the source of truth.
- ``export_to_sqlite``: writes the PostgreSQL state of one workspace back into
  the SQLite control plane (updates, new tasks, deletions, and links), so a
  rollback keeps everything written after the cutover.

Runbook: docs/runbooks/tasks-cutover-and-rollback.md
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from empire_platform.devdb import SUPERUSER, Cluster, pg_bin_dir

FROZEN_TABLES = (
    "tasks",
    "task_assignments",
    "task_goal_links",
    "task_project_links",
    "task_context_links",
    "task_skill_links",
    "task_role_suggestions",
    "task_types",
)
LINK_TABLES = {
    "goal": ("task_goal_links", "goal_id"),
    "project": ("task_project_links", "project_id"),
    "context": ("task_context_links", "context_id"),
    "skill": ("task_skill_links", "skill_id"),
}


class ToolFailed(RuntimeError):
    """A PostgreSQL client tool failed; the message never contains the connection string."""


def _run_tool(args: list[str], conninfo: str) -> None:
    """Run pg_dump/pg_restore; the connection string never appears in an error or traceback."""
    result = subprocess.run(
        [*args, "--dbname", conninfo],
        capture_output=True,
        text=True,
        check=False,  # returncode is checked below without echoing the connection string
    )
    if result.returncode != 0:
        detail = re.sub(r"postgres(ql)?://\S+", "<connection>", result.stderr or "")[
            -400:
        ]
        raise ToolFailed(
            f"{Path(args[0]).name} exited {result.returncode}: {detail.strip()}"
        )


def backup(cluster: Cluster, path: Path, dbname: str = "empire") -> Path:
    _run_tool(
        [str(pg_bin_dir() / "pg_dump"), "-Fc", "-f", str(path)],
        cluster.conninfo(SUPERUSER, dbname),
    )
    return path


def restore(cluster: Cluster, path: Path, dbname: str) -> None:
    """Restore into a new database ``dbname`` (never over the live one)."""
    with cluster.connect(SUPERUSER, "postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}" OWNER empire_migrator')
    _run_tool(
        [str(pg_bin_dir() / "pg_restore"), "--exit-on-error", str(path)],
        cluster.conninfo(SUPERUSER, dbname),
    )


def set_writes(cluster: Cluster, enabled: bool, dbname: str = "empire") -> None:
    with cluster.connect("empire_migrator", dbname) as conn:
        conn.execute(
            "UPDATE tasks.settings SET value = %s WHERE key = 'writes_enabled'",
            ("true" if enabled else "false",),
        )
        conn.commit()


def freeze_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        for table in FROZEN_TABLES:
            for op in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS empire_freeze_{table}_{op.lower()} BEFORE {op} ON {table}"
                    " BEGIN SELECT RAISE(ABORT, 'tasks moved to empire-platform; write through the Tasks API'); END"
                )


def unfreeze_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        for table in FROZEN_TABLES:
            for op in ("insert", "update", "delete"):
                conn.execute(f"DROP TRIGGER IF EXISTS empire_freeze_{table}_{op}")


@dataclass
class ExportReport:
    updated: int = 0
    inserted: int = 0
    deleted: int = 0
    links_inserted: int = 0
    links_unmapped: list[str] = field(default_factory=list)
    new_legacy_ids: dict[str, int] = field(default_factory=dict)


def _sqlite_time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if value else None


def _row_values(task: dict[str, Any], ids: dict[str, int]) -> dict[str, Any]:
    parent = lambda key: ids.get(str(task[key])) if task[key] else None
    return {
        "title": task["title"],
        "description": task["description"],
        "status": task["status"],
        "priority": task["priority"],
        "project": task["project"],
        "tags": json.dumps(task["tags"]),
        "category": task["category"],
        "due_date": task["due_date"].isoformat() if task["due_date"] else None,
        "completed_at": _sqlite_time(task["completed_at"]),
        "external_id": task["external_id"],
        "external_source": task["external_source"],
        "external_url": task["external_url"],
        "notes": task["notes"],
        "created_at": _sqlite_time(task["created_at"]),
        "updated_at": _sqlite_time(task["updated_at"]),
        "reminder_at": _sqlite_time(task["reminder_at"]),
        "reminder_sent": int(task["reminder_sent"]),
        "reminder_channels": task["reminder_channels"],
        "estimated_minutes": task["estimated_minutes"],
        "actual_minutes": task["actual_minutes"],
        "started_at": _sqlite_time(task["started_at"]),
        "recurrence_rule": task["recurrence_rule"],
        "is_recurring_instance": int(task["is_recurring_instance"]),
        "recurrence_occurrence": task["recurrence_occurrence"],
        "parent_recurring_id": parent("parent_recurring_id"),
        "archived_at": _sqlite_time(task["archived_at"]),
        "archive_reason": task["archive_reason"],
        "parent_task_id": parent("parent_task_id"),
        "calendar_event_id": task["calendar_event_id"],
        "progress_percent": task["progress_percent"],
    }


def export_to_sqlite(
    cluster: Cluster, workspace_id: str, sqlite_path: Path, dbname: str = "empire"
) -> ExportReport:
    """Make SQLite match PostgreSQL for one workspace's tasks, in one SQLite transaction.

    Run with the Tasks API write switch off and the SQLite freeze triggers
    still installed; the triggers are bypassed here by dropping and recreating
    them inside the same transaction.
    """
    report = ExportReport()
    with cluster.connect("empire_migrator", dbname) as pg:
        pg.row_factory = dict_row
        pg.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
        tasks = pg.execute(
            "SELECT * FROM tasks.tasks ORDER BY created_at, id"
        ).fetchall()
        links = pg.execute(
            "SELECT * FROM tasks.task_links ORDER BY created_at, id"
        ).fetchall()

    src = sqlite3.connect(sqlite_path, isolation_level=None)
    src.execute("BEGIN IMMEDIATE")
    try:
        frozen = [
            row[0]
            for row in src.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'empire_freeze_%'"
            )
        ]
        src.execute("DROP TRIGGER IF EXISTS empire_freeze_tasks_insert")
        for statement in ("update", "delete"):
            src.execute(f"DROP TRIGGER IF EXISTS empire_freeze_tasks_{statement}")
        for table in FROZEN_TABLES[1:]:
            for op in ("insert", "update", "delete"):
                src.execute(f"DROP TRIGGER IF EXISTS empire_freeze_{table}_{op}")

        # Task numbers are the SQLite IDs, for imported and new tasks alike.
        ids = {str(task["id"]): task["legacy_id"] for task in tasks}
        present = {row[0] for row in src.execute("SELECT id FROM tasks")}
        for task in tasks:
            if task["legacy_id"] not in present:
                src.execute(
                    "INSERT INTO tasks (id, title) VALUES (?, ?)",
                    (task["legacy_id"], task["title"]),
                )
                report.new_legacy_ids[str(task["id"])] = task["legacy_id"]
                report.inserted += 1
        for task in tasks:
            values = _row_values(task, ids)
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- local SQLite cutover: table/column names come from fixed mappings (_row_values keys, LINK_TABLES); values are bound as ? parameters
            src.execute(
                f"UPDATE tasks SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                [*values.values(), ids[str(task["id"])]],
            )
            if task["legacy_id"] in present:
                report.updated += 1
        keep = set(ids.values())
        for (legacy,) in src.execute("SELECT id FROM tasks").fetchall():
            if legacy not in keep:
                src.execute("DELETE FROM tasks WHERE id = ?", (legacy,))
                report.deleted += 1
        for link in links:
            if link["legacy_id"] is not None:
                continue  # imported from SQLite, so SQLite still has it
            target = LINK_TABLES.get(link["target_type"])
            ref = link["target_ref"].rsplit(":", 1)[-1]
            if target is None or not ref.startswith("legacy-"):
                report.links_unmapped.append(
                    f"{link['id']}: {link['relation']} -> {link['target_ref']}"
                )
                continue
            table, column = target
            extra = {"source": "empire-platform"}
            if table == "task_skill_links":
                extra["match_reason"] = "linked in empire-platform"
            columns = ["task_id", column, *extra]
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- local SQLite cutover: table/column names come from fixed mappings (_row_values keys, LINK_TABLES); values are bound as ? parameters
            src.execute(
                f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [
                    ids[str(link["task_id"])],
                    int(ref.removeprefix("legacy-")),
                    *extra.values(),
                ],
            )
            report.links_inserted += 1
        for statement in frozen:
            src.execute(statement)
        src.execute("COMMIT")
    except Exception:
        src.execute("ROLLBACK")
        raise
    finally:
        src.close()
    return report
