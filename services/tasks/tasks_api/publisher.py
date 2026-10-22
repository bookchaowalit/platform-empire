"""Transactional outbox publisher for the tasks cell.

Runs as ``tasks_publisher``, which can read and mark outbox rows in every
workspace and nothing else. Events go out oldest first; a failed delivery is
retried later with exponential backoff. Delivery is at least once: if the
publisher stops between delivering and marking, the event is delivered again,
so every consumer must be idempotent on the event ID.

    empire-publish [--batch N] [--loop SECONDS]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from empire_platform._sentry import capture

MAX_BACKOFF_SECONDS = 600


@dataclass
class PublishReport:
    delivered: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def publish(
    connect: Callable[[], psycopg.Connection[Any]],
    deliver: Callable[[dict[str, Any]], Any],
    *,
    batch: int = 100,
) -> PublishReport:
    report = PublishReport()
    with connect() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            "SELECT id, workspace_id, type, version, subject, execution_id, classification, payload, occurred_at,"
            " correlation_id"
            " FROM tasks.outbox WHERE published_at IS NULL AND next_attempt_at <= now()"
            " ORDER BY occurred_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        ).fetchall()
        reported = False  # one Sentry event per sweep, not one per failed row
        for row in rows:
            event = json.loads(json.dumps(row, default=str))
            try:
                deliver(event)
            except Exception as exc:  # noqa: BLE001 - any consumer failure: retry later, never lose the event
                if not reported:
                    capture(exc)
                    reported = True
                report.failed += 1
                report.errors.append(f"{row['id']}: {type(exc).__name__}")
                conn.execute(
                    "UPDATE tasks.outbox SET attempts = attempts + 1, last_error = %s,"
                    " next_attempt_at = now() + make_interval(secs => least(%s, power(2, attempts + 1)))"
                    " WHERE id = %s",
                    (type(exc).__name__, MAX_BACKOFF_SECONDS, row["id"]),
                )
                continue
            conn.execute(
                "UPDATE tasks.outbox SET published_at = now(), last_error = NULL WHERE id = %s",
                (row["id"],),
            )
            report.delivered += 1
        conn.commit()
    return report


def run_loop(
    step: Callable[[], PublishReport],
    interval: float,
    stop: threading.Event,
    *,
    max_iterations: int | None = None,
    max_backoff: float = 300.0,
    log: Callable[[str], Any] | None = None,
) -> int:
    """Repeat ``step`` until ``stop`` is set; one JSON line of counts per batch.

    A failed delivery is retried by the outbox itself; a database error here
    backs off exponentially and the loop keeps going. Returns the iterations run.
    """
    log = log or (lambda line: print(line, flush=True))
    errors = 0
    iterations = 0
    while not stop.is_set() and (max_iterations is None or iterations < max_iterations):
        iterations += 1
        try:
            report = step()
        except Exception as exc:  # noqa: BLE001 - transient database or network error: back off, never crash-loop
            if not errors:  # one Sentry event per failure streak, not one per retry
                capture(exc)
            errors += 1
            delay = min(max_backoff, max(interval, 1.0) * 2 ** min(errors, 10))
            log(
                json.dumps(
                    {
                        "delivered": 0,
                        "failed": 0,
                        "error": type(exc).__name__,
                        "retry_in": delay,
                    }
                )
            )
        else:
            errors = 0
            delay = interval
            log(json.dumps({"delivered": report.delivered, "failed": report.failed}))
        stop.wait(delay)
    return iterations


def main(argv: list[str] | None = None) -> int:
    from graph_service.consumer import GraphConsumer

    from empire_platform.devdb import default_cluster

    parser = argparse.ArgumentParser(
        prog="empire-publish", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument(
        "--loop",
        type=float,
        metavar="SECONDS",
        help="repeat forever, sleeping SECONDS between batches",
    )
    args = parser.parse_args(argv)
    cluster = default_cluster()
    consumer = GraphConsumer(lambda: cluster.connect("graph_api"))

    def step() -> PublishReport:
        return publish(
            lambda: cluster.connect("tasks_publisher"), consumer, batch=args.batch
        )

    if args.loop is None:
        report = step()
        print(
            json.dumps(
                {
                    "delivered": report.delivered,
                    "failed": report.failed,
                    "errors": report.errors,
                }
            )
        )
        return 0 if not report.failed else 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if os.environ.get("PORT"):
        serve_health(int(os.environ["PORT"]), stop)
    run_loop(step, args.loop, stop)
    return 0


def serve_health(port: int, stop: threading.Event) -> None:
    """/health for the platform's deploy check: 200 while the loop runs, 503 once it stops."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            healthy = self.path == "/health" and not stop.is_set()
            self.send_response(200 if healthy else 503)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}' if healthy else b"")

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
