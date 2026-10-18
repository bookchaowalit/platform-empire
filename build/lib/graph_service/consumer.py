"""Apply cell events to the graph; safe to call more than once per event."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg


class GraphConsumer:
    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect

    def __call__(self, event: dict[str, Any]) -> str:
        with self._connect() as conn:
            # Bind to the event's workspace; ingest_event refuses a mismatch.
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(event["workspace_id"]),),
            )
            payload = json.dumps(event, default=str)
            outcome = conn.execute(
                "SELECT graph.ingest_event(%s::jsonb)", (payload,)
            ).fetchone()[0]
            if outcome != "duplicate":
                conn.execute("SELECT graph.record_correlation(%s::jsonb)", (payload,))
            conn.commit()
            return outcome
