"""Small performance and failure checks; measurements go to .runtime/readiness/.

Not a load test: it shows concurrent writes stay correct, a backlog drains,
recovery after a graph outage is bounded, and connection exhaustion fails
fast instead of hanging.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from graph_service.consumer import GraphConsumer
from graph_world import apps, bearer, due_now, gkeys, gtoken, gworld, run_publisher  # noqa: F401  (fixtures)

METRICS = (
    Path(__file__).resolve().parents[1] / ".runtime" / "readiness" / "resilience.json"
)


@pytest.fixture(scope="module")
def metrics():
    values: dict = {}
    yield values
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(METRICS.read_text()) if METRICS.exists() else {}
    METRICS.write_text(json.dumps({**existing, **values}, indent=2, sort_keys=True))


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        return conn.execute(query, params).fetchall()


def create_many(
    tasks, headers, count: int, prefix: str, workers: int = 16
) -> list[dict]:
    def one(i: int) -> dict:
        response = tasks.post(
            "/v1/tasks", json={"title": f"{prefix} {i}"}, headers=headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, range(count)))


def drain(cluster, deliver=None) -> tuple[int, float]:
    started, delivered = time.monotonic(), 0
    while True:
        report = run_publisher(cluster, deliver)
        delivered += report.delivered
        if report.delivered == 0 and report.failed == 0:
            return delivered, time.monotonic() - started


def test_concurrent_writes_get_unique_contiguous_numbers(apps, gworld, gtoken, metrics):
    tasks, _ = apps
    headers = bearer(gtoken(gworld.owner, gworld.ws))
    started = time.monotonic()
    made = create_many(tasks, headers, 40, "Concurrent")
    metrics["concurrent_creates"] = {
        "count": 40,
        "workers": 16,
        "seconds": round(time.monotonic() - started, 3),
    }
    numbers = sorted(task["number"] for task in made)
    assert len(set(numbers)) == 40
    assert numbers == list(range(numbers[0], numbers[0] + 40))


def test_backlog_drains_completely(cluster, apps, gworld, gtoken, metrics):
    tasks, _ = apps
    drain(cluster)  # start from an empty queue
    made = create_many(tasks, bearer(gtoken(gworld.owner, gworld.ws)), 200, "Backlog")
    backlog = superuser(
        cluster, "SELECT count(*) FROM tasks.outbox WHERE published_at IS NULL"
    )[0][0]
    delivered, seconds = drain(cluster)
    metrics["backlog_drain"] = {
        "events": backlog,
        "seconds": round(seconds, 3),
        "events_per_second": round(backlog / seconds, 1) if seconds else None,
    }
    assert delivered >= backlog >= 200
    urns = [task["urn"] for task in made]
    assert (
        superuser(
            cluster, "SELECT count(*) FROM graph.nodes WHERE urn = ANY(%s)", (urns,)
        )[0][0]
        == 200
    )


def test_recovery_after_graph_outage_is_bounded(cluster, apps, gworld, gtoken, metrics):
    tasks, _ = apps
    drain(cluster)

    def down(_event):
        raise psycopg.OperationalError("graph unavailable")

    made = create_many(tasks, bearer(gtoken(gworld.owner, gworld.ws)), 100, "Outage")
    assert run_publisher(cluster, down).delivered == 0
    due_now(cluster)
    delivered, seconds = drain(
        cluster, GraphConsumer(lambda: cluster.connect("graph_api"))
    )
    metrics["recovery_after_outage"] = {
        "events": delivered,
        "seconds": round(seconds, 3),
    }
    assert delivered >= 100
    assert seconds < 60
    urns = [task["urn"] for task in made]
    assert (
        superuser(
            cluster, "SELECT count(*) FROM graph.nodes WHERE urn = ANY(%s)", (urns,)
        )[0][0]
        == 100
    )


def test_connection_exhaustion_fails_fast(cluster, apps, gworld, gtoken, metrics):
    tasks, _ = apps
    headers = bearer(gtoken(gworld.owner, gworld.ws))
    with cluster.connect("postgres", autocommit=True) as admin:
        admin.execute("ALTER ROLE tasks_api CONNECTION LIMIT 2")
    held = [cluster.connect("tasks_api") for _ in range(2)]
    try:
        started = time.monotonic()
        response = tasks.post("/v1/tasks", json={"title": "No room"}, headers=headers)
        elapsed = time.monotonic() - started
        metrics["connection_exhaustion"] = {
            "status": response.status_code,
            "seconds": round(elapsed, 3),
        }
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "unavailable"
        assert elapsed < 2
    finally:
        for conn in held:
            conn.close()
        with cluster.connect("postgres", autocommit=True) as admin:
            admin.execute("ALTER ROLE tasks_api CONNECTION LIMIT -1")
    assert (
        tasks.post(
            "/v1/tasks", json={"title": "Room again"}, headers=headers
        ).status_code
        == 201
    )
