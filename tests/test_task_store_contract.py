"""Solo Empire's TaskStore adapter behaves the same on SQLite and on the Tasks API.

The adapter lives in the Solo Empire repository (infra/scripts/utils/task_store.py); its SQLite
backend is retired and kept only as a test double (infra/tests/support/sqlite_task_store.py);
this test runs one writer scenario against both backends over real HTTP and
compares the outcomes. Skipped when this checkout is not inside Solo Empire.
"""

from __future__ import annotations

import socket
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from conftest import create_workspace
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER

ADAPTER_DIR = next(
    (
        p / "infra" / "scripts" / "utils"
        for p in Path(__file__).resolve().parents
        if (p / "infra" / "scripts" / "utils" / "task_store.py").is_file()
    ),
    None,
)
pytestmark = pytest.mark.skipif(
    ADAPTER_DIR is None, reason="not inside a Solo Empire checkout"
)
SCHEMA = Path(__file__).parent / "fixtures" / "sqlite_tasks_schema.sql"
ISSUER = "empire-store-contract"


@pytest.fixture(scope="module")
def task_store():
    sys.path.insert(0, str(ADAPTER_DIR))
    import task_store as module

    yield module
    sys.path.remove(str(ADAPTER_DIR))


@pytest.fixture(scope="module")
def sqlite_store():
    """The SQLite task backend was retired on 2026-10-09; it survives only as a test double in Solo Empire."""
    support = ADAPTER_DIR.parents[1] / "tests" / "support"
    sys.path.insert(0, str(support))
    import sqlite_task_store

    yield sqlite_task_store.SqliteTaskStore
    sys.path.remove(str(support))


@pytest.fixture(scope="module")
def api_url(cluster, tmp_path_factory):
    private_pem, public_pem = key_pair(tmp_path_factory.mktemp("store-keys"))
    app = create_app(
        lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", private_pem
    server.should_exit = True
    thread.join(timeout=5)


def scenario(store) -> dict:
    """What a writer does, through the store only. Returns backend-neutral results."""
    out: dict = {}
    out["upsert"] = [
        store.upsert_external(
            "engagements",
            "E-1",
            {
                "title": "Next action",
                "priority": 2,
                "tags": "client,oak",
                "due_date": "2026-10-20",
            },
        )[0],
        store.upsert_external(
            "engagements",
            "E-1",
            {
                "title": "Next action",
                "priority": 2,
                "tags": "client,oak",
                "due_date": "2026-10-20",
            },
        )[0],
        store.upsert_external(
            "engagements", "E-1", {"title": "Next action", "priority": 1}
        )[0],
    ]
    found = store.find_external("engagements", "E-1")
    out["found"] = (
        found["title"],
        found["priority"],
        found["tags"],
        found["due_date"],
        found["status"],
    )
    out["missing"] = store.find_external("engagements", "nope")

    weekly = store.create(
        {
            "title": "Weekly run",
            "recurrence_rule": "weekly",
            "due_date": "2026-10-01",
            "estimated_minutes": 30,
        }
    )
    first = store.generate_occurrences(weekly, "2026-10-15")
    out["occurrences"] = [
        (store.get(n)["title"], store.get(n)["due_date"]) for n in first
    ]
    out["occurrence_columns"] = [
        (
            store.get(n)["recurrence_rule"],
            store.get(n)["estimated_minutes"],
            store.get(n)["is_recurring_instance"],
        )
        for n in first
    ]
    out["occurrences_retry"] = store.generate_occurrences(weekly, "2026-10-15")

    reminder = store.create({"title": "Stretch", "reminder_at": "2026-10-10 09:00:00"})
    out["reminders_due"] = [
        t["title"] for t in store.list(reminder_due_before="2026-10-10 10:00:00")
    ]
    out["reminder"] = [
        store.record_reminder_delivered(reminder, "2026-10-10 09:00:00", "line"),
        store.record_reminder_delivered(reminder, "2026-10-10 09:00:00", "line"),
    ]
    out["reminders_due_after"] = [
        t["title"] for t in store.list(reminder_due_before="2026-10-10 10:00:00")
    ]

    moved = store.create({"title": "Overdue", "due_date": "2026-09-01"})
    out["overdue"] = [
        t["title"] for t in store.list(status=["todo"], due_before="2026-10-01")
    ]
    store.update(moved, {"due_date": "2026-11-01"})
    cancelled = store.create({"title": "Gone upstream"})
    store.update(cancelled, {"status": "cancelled"})
    done = store.create({"title": "Finished"})
    store.complete(done)
    store.update(store.create({"title": "Calendar"}), {"calendar_event_id": "cal-9"})
    old = store.create({"title": "Old done"})
    store.complete(old)
    store.archive(old, "older than 30 days")
    restored = store.create({"title": "Archived by mistake"})
    store.archive(restored, "mistake")
    store.unarchive(restored)

    def view(task):
        return (
            task["title"],
            task["status"],
            task["due_date"],
            bool(task["completed_at"]),
            task["calendar_event_id"],
            bool(task["archived_at"]),
        )

    out["active"] = sorted(view(t) for t in store.list())
    out["with_archived"] = sorted(view(t) for t in store.list(include_archived=True))
    out["missing_calendar"] = sorted(
        t["title"] for t in store.list(missing_calendar_event=True)
    )
    return out


def read_scenario(store) -> dict:
    """What the readers and relation writers do, through the store only."""
    from datetime import date, timedelta

    today = date.today()
    out: dict = {}
    a = store.create(
        {
            "title": "Report 100% done",
            "project": "alpha",
            "priority": 1,
            "due_date": (today - timedelta(days=1)).isoformat(),
        }
    )
    b = store.create(
        {
            "title": "Plan",
            "description": "Book Dev loop",
            "project": "alpha",
            "priority": 3,
            "due_date": (today + timedelta(days=3)).isoformat(),
        }
    )
    c = store.create({"title": "Ops", "category": "ops", "priority": 2})
    store.complete(c)
    store.update(b, {"progress_percent": 40})
    child = store.create({"title": "Child of plan", "parent_task_id": b})
    out["parent"] = (
        store.get(child)["parent_task_id"] == b,
        [r["id"] == child for r in store.list(parent=b)],
    )
    store.upsert_external("seed", "S-1", {"title": "Seeded child", "parent_task_id": a})
    out["parent_upsert"] = store.find_external("seed", "S-1")["parent_task_id"] == a

    def numbers(rows):
        return sorted(r["title"] for r in rows)

    out["project"] = numbers(store.list(project="alpha"))
    out["priority"] = numbers(store.list(priority_max=2))
    out["due_window"] = numbers(
        store.list(
            due_from=today.isoformat(),
            due_before=(today + timedelta(days=7)).isoformat(),
        )
    )
    out["numbers"] = numbers(store.list(numbers=[a, c]))
    out["search"] = (
        numbers(store.list(q="book dev"))
        + numbers(store.list(q="100%"))
        + numbers(store.list(q="_"))
    )
    out["completed_today"] = numbers(
        store.list(
            status=["done"], completed_between=(today.isoformat(), today.isoformat())
        )
    )
    out["created_today"] = (
        len(store.list(created_between=(today.isoformat(), today.isoformat()))) >= 3
    )
    out["progress"] = store.get(b)["progress_percent"]
    out["stats"] = store.stats(group_by="status", numbers=[a, b, c])
    out["stats_project"] = store.stats(group_by="project", project="alpha")

    out["link"] = [
        store.link(a, "goal", 7, {"link_type": "primary", "source": "seed"}),
        store.link(a, "goal", 7, {"link_type": "supporting"}),
        store.link(
            a, "goal", 7, {"link_type": "supporting", "source": "seed"}, update=True
        ),
        store.link(
            b,
            "project",
            3,
            {"relation_type": "delivery", "weight": 1.0, "source": "seed"},
        ),
        store.link(
            b,
            "skill",
            11,
            {
                "link_type": "suggested",
                "match_reason": "plan",
                "source_registry": "docs/ROLE-LEARNING.yaml",
            },
        ),
    ]

    def strip(rows):
        return [
            (
                r["task"] == a,
                r["target_type"],
                r["target_id"],
                r["relation"],
                sorted(r["attributes"].items()),
            )
            for r in rows
        ]

    out["links_goal"] = strip(store.links(target_type="goal", target_id=7))
    out["links_task_b"] = strip(store.links(task=b))

    out["assign"] = [
        store.assign(b, "role", "role.engineer", "Engineer"),
        store.assign(b, "role", "role.engineer"),
        store.assign(b, "user", "owner", "Owner"),
    ]
    out["assignments"] = [
        (r["task"] == b, r["assignee_type"], r["assignee_id"], r["assignment_type"])
        for r in store.assignments(assignee_type="role")
    ]
    out["assignments_b"] = sorted(
        (r["assignee_type"], r["assignee_id"]) for r in store.assignments(task=b)
    )

    registry = "docs/ROLE-TASK-ROUTING.yaml"
    out["suggest"] = [
        store.suggest_role(a, "role.writer", registry, "high", "report"),
        store.suggest_role(a, "role.writer", registry, "high", "report"),
        store.review_role(
            a, "role.writer", registry, "accepted", expected_status="suggested"
        ),
        store.review_role(
            a, "role.writer", registry, "accepted", expected_status="suggested"
        ),
        store.suggest_role(a, "role.writer", registry, "medium", "report and plan"),
    ]
    out["suggestions"] = [
        (r["task"] == a, r["role_id"], r["confidence"], r["review_status"])
        for r in store.role_suggestions(source_registry=registry)
    ]
    store.suggest_role(b, "role.stale", registry, "low", "old match")
    out["withdraw"] = [
        store.withdraw_role(b, "role.stale", registry, only_status="suggested"),
        store.withdraw_role(a, "role.writer", registry, only_status="suggested"),
        store.unlink(b, "skill", 11),
        store.unlink(b, "skill", 11),
    ]
    out["after_withdraw"] = (
        len(store.role_suggestions(source_registry=registry)),
        len(store.links(task=b)),
    )
    out["dependency"] = [store.add_dependency(b, a), store.add_dependency(b, a)]
    out["dependencies"] = [
        (d["task"] == b, d["depends_on"] == a, d["dependency_type"])
        for d in store.dependencies(task=b)
    ]
    return out


def test_read_and_relation_methods_agree(
    task_store, sqlite_store, api_url, cluster, tmp_path
):
    db = sqlite3.connect(tmp_path / "legacy-read.db", isolation_level=None)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    legacy = read_scenario(sqlite_store(db))

    url, private_pem = api_url
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"store-read-{uuid.uuid4().hex[:8]}", list(TASKS_OWNER)
        )
        conn.commit()
    api = read_scenario(
        task_store.ApiTaskStore(
            url, mint(private_pem, sub=ws.owner, ws=ws.id, issuer=ISSUER)
        )
    )

    assert legacy["search"] == ["Plan", "Report 100% done"]
    assert legacy["parent"] == (True, [True])
    assert legacy["parent_upsert"] is True
    assert legacy["stats"] == {"total": 3, "groups": {"todo": 2, "done": 1}}
    assert legacy["link"] == ["created", "unchanged", "updated", "created", "created"]
    assert legacy["assign"] == ["created", "unchanged", "created"]
    assert legacy["suggest"] == ["created", "unchanged", True, False, "updated"]
    assert legacy["suggestions"] == [(True, "role.writer", "medium", "accepted")]
    assert legacy["withdraw"] == [True, False, True, False]
    assert legacy["after_withdraw"] == (1, 1)
    assert legacy["dependency"] == ["created", "unchanged"]
    assert legacy["dependencies"] == [(True, True, "finish_to_start")]
    for key in legacy:
        assert api[key] == legacy[key], key


def test_sqlite_and_api_backends_agree(
    task_store, sqlite_store, api_url, cluster, tmp_path
):
    db = sqlite3.connect(tmp_path / "legacy.db", isolation_level=None)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    legacy = scenario(sqlite_store(db))

    url, private_pem = api_url
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(conn, f"store-{uuid.uuid4().hex[:8]}", list(TASKS_OWNER))
        conn.commit()
    token = mint(private_pem, sub=ws.owner, ws=ws.id, issuer=ISSUER)
    api = scenario(task_store.ApiTaskStore(url, token, channel="cli"))

    assert legacy["upsert"] == ["created", "unchanged", "updated"]
    assert legacy["found"] == (
        "Next action",
        1,
        ["client", "oak"],
        "2026-10-20",
        "todo",
    )
    assert legacy["occurrences"] == [
        ("[R1] Weekly run", "2026-10-08"),
        ("[R2] Weekly run", "2026-10-15"),
    ]
    assert legacy["occurrence_columns"] == [("weekly", 30, True)] * 2
    assert legacy["reminder"] == [True, False]
    assert legacy["reminders_due_after"] == []
    for key in legacy:
        assert api[key] == legacy[key], key


def test_api_backend_reports_permission_errors(task_store, api_url, cluster):
    url, private_pem = api_url
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"store-deny-{uuid.uuid4().hex[:8]}", ["tasks.task:read"]
        )
        conn.commit()
    store = task_store.ApiTaskStore(
        url, mint(private_pem, sub=ws.owner, ws=ws.id, issuer=ISSUER)
    )
    with pytest.raises(task_store.TaskStoreError) as error:
        store.create({"title": "x"})
    assert error.value.status == 403
    assert error.value.reason == "no_permission"


def test_store_rejects_unknown_fields(sqlite_store, tmp_path):
    db = sqlite3.connect(tmp_path / "legacy.db")
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="sync_status"):
        sqlite_store(db).create({"title": "x", "sync_status": "synced"})
