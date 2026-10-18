"""Mirror answered decisions to platform-identity and log whether it agrees.

Migration step 3 (platform-identity design §7): the identity service runs the
same decision point in shadow mode. After this process has answered a
``POST /v1/authz/decide``, the same request (body, bearer token, channel) is
queued for one background worker that sends it to the identity service and
compares the two answers. The caller never waits for it and never sees its
answer; when the queue is full the comparison is dropped (and counted), so a
slow identity service can never take request threads away from this process.

The log line carries the action, both decisions and reasons only: no token,
resource id, actor, or workspace. Turn the mirror off by unsetting
``IDENTITY_SHADOW_URL`` (rollback for this step).
"""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from empire_platform._sentry import capture

# The shadow runs once per authorization decision: report each failure kind at most once an hour.
_REPORT_INTERVAL_SECONDS = 3600.0
_last_reported: dict[str, float] = {}


def _report(kind: str, exc: BaseException) -> None:
    now = time.monotonic()
    if (
        now - _last_reported.get(kind, -_REPORT_INTERVAL_SECONDS)
        >= _REPORT_INTERVAL_SECONDS
    ):
        _last_reported[kind] = now
        capture(exc)


log = logging.getLogger("platform_api.identity_shadow")
TIMEOUT = httpx.Timeout(2.0, connect=1.0, pool=0.5)
QUEUE_SIZE = 256
_UNSAFE = re.compile(r"[^a-z0-9_.:-]")


def _code(value: object) -> str:
    """A decision or reason from the other service, reduced to a short code before it is logged."""
    return _UNSAFE.sub("?", str(value).lower())[:40]


def enable_logging() -> None:
    """Every comparison is one line on stderr (uvicorn configures only its own loggers)."""
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def shadow_url(raw: str | None) -> str | None:
    """The decide URL of the identity service, or None when the mirror is off."""
    if raw is None or not raw.strip():
        return None
    base = raw.strip().rstrip("/")
    parts = urlsplit(base)
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if not (parts.scheme == "https" and parts.hostname) and not local:
        raise SystemExit("IDENTITY_SHADOW_URL must be an HTTPS URL")
    return f"{base}/v1/authz/decide"


class IdentityShadow:
    """Callable handed to the decide route; ``start=False`` keeps it synchronous for tests."""

    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        *,
        start: bool = True,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self.url = url
        self.client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.dropped = 0
        self._queue: queue.Queue[tuple[Any, ...]] | None = None
        if start:
            self._queue = queue.Queue(maxsize=queue_size)
            threading.Thread(
                target=self._work, name="identity-shadow", daemon=True
            ).start()

    def __call__(
        self,
        body: dict[str, Any],
        authorization: str | None,
        channel: str | None,
        decision: str,
        reason: str,
    ) -> None:
        job = (body, authorization, channel, decision, reason)
        if self._queue is None:
            self.compare(*job)
            return
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("identity_shadow outcome=dropped total=%s", self.dropped)

    def _work(self) -> None:
        assert self._queue is not None
        while True:
            job = self._queue.get()
            try:
                self.compare(*job)
            except Exception as exc:  # noqa: BLE001 - the mirror must never stop or surface errors
                _report("error", exc)
                log.warning(
                    "identity_shadow outcome=error error=%s", type(exc).__name__
                )
            finally:
                self._queue.task_done()

    def compare(
        self,
        body: dict[str, Any],
        authorization: str | None,
        channel: str | None,
        decision: str,
        reason: str,
    ) -> None:
        action = body.get("action")
        headers = {"Authorization": authorization or ""}
        if channel is not None:
            headers["x-empire-channel"] = channel
        try:
            response = self.client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            _report("unreachable", exc)
            log.warning(
                "identity_shadow outcome=unreachable action=%s error=%s",
                action,
                type(exc).__name__,
            )
            return
        if response.status_code != 200:
            log.warning(
                "identity_shadow outcome=mismatch action=%s primary=%s/%s shadow_status=%s",
                action,
                decision,
                reason,
                response.status_code,
            )
            return
        try:
            shadow = response.json()
            shadow_decision, shadow_reason = shadow["decision"], shadow["reason"]
        except (ValueError, KeyError, TypeError) as exc:
            _report("invalid_response", exc)
            log.warning("identity_shadow outcome=invalid_response action=%s", action)
            return
        outcome = (
            "match"
            if (shadow_decision, shadow_reason) == (decision, reason)
            else "mismatch"
        )
        level = logging.INFO if outcome == "match" else logging.WARNING
        log.log(
            level,
            "identity_shadow outcome=%s action=%s primary=%s/%s shadow=%s/%s",
            outcome,
            action,
            decision,
            reason,
            _code(shadow_decision),
            _code(shadow_reason),
        )
