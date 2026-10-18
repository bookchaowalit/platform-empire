"""Generate the due instances of recurring tasks through the Tasks API.

Runs as the ``recurring-gen`` Railway cron service (daily), replacing the retired
local cron runner job "Generate Recurring". Every read and write goes through the
Tasks API with the ``recurring-gen`` automation token (scopes ``tasks.task:read``
and ``tasks.occurrence:write``), so the platform write path (executions, audit,
outbox) applies. The API creates occurrences idempotently
(``POST /v1/tasks/{id}/occurrences/generate`` has a unique
``(parent_recurring_id, recurrence_occurrence)`` key), so a re-run, or an
overlapping run, creates nothing twice.

    empire-recurring [--dry-run] [--until YYYY-MM-DD]

``--dry-run`` lists what would be generated and writes nothing. One broken
parent is logged and skipped; the exit code is non-zero only when the API is
unreachable or a parent failed (exit 1), or configuration is missing (exit 2).
Zero recurring tasks is a normal, successful run.

Environment: ``EMPIRE_TASKS_API_URL``, ``RECURRING_GEN_TOKEN``. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ._sentry import capture

BANGKOK = ZoneInfo("Asia/Bangkok")
PAGE = 200
MAX_PREVIEW = 366  # matches the API cap per generate call
# Same rule language as the Tasks API (services/tasks/tasks_api), used only for the dry-run preview.
FIXED_DAYS = {
    "daily": 1,
    "every day": 1,
    "weekly": 7,
    "every week": 7,
    "monthly": 30,
    "every month": 30,
    "biweekly": 14,
    "every 2 weeks": 14,
    "every 3 months": 90,
}
PATTERNS = (
    (r"every\s+(\d+)\s*months?", 30),
    (r"every\s+(\d+)\s*d", 1),
    (r"every\s+(\d+)\s*w", 7),
)
INSTANCE_TITLE = re.compile(r"^\[R(\d+)\] (.*)$", re.DOTALL)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):  # never forward a token
        return None


class RequestError(RuntimeError):
    """The request could not be built or sent; the message is fixed so no header value can leak."""

    def __init__(self) -> None:
        super().__init__("request_failed")


def bad_value(value: str) -> bool:
    """True when ``value`` has whitespace or control characters (CR/LF would reach the header)."""
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


class ApiError(RuntimeError):
    def __init__(self, status: int, where: str) -> None:
        super().__init__(f"{where}: HTTP {status}")
        self.status = status


class Http:
    """Bearer JSON client for the Tasks API (https only, no redirects)."""

    def __init__(self, base: str, token: str, timeout: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != "https" and parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise SystemExit("EMPIRE_TASKS_API_URL must be https")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(
        self, method: str, path: str, body: Any = None, params: dict | None = None
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        query = f"?{urllib.parse.urlencode(clean, doseq=True)}" if clean else ""
        try:
            request = urllib.request.Request(
                f"{self.base}{path}{query}",
                method=method,
                data=json.dumps(body, default=str).encode()
                if body is not None
                else None,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Empire-Channel": "workflow",
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise ApiError(
                error.code,
                f"{method} {path.split('/occurrences')[0] if 'occurrences' in path else path}",
            ) from None
        except (ValueError, TypeError, http.client.HTTPException):
            raise RequestError() from None  # never include the exception text: it can quote the header


def recurrence_step(rule: str | None) -> timedelta | None:
    if not rule:
        return None
    rule = rule.strip().lower()
    if rule in FIXED_DAYS:
        return timedelta(days=FIXED_DAYS[rule])
    for pattern, days in PATTERNS:
        match = re.match(pattern, rule)
        if match:
            try:
                step = timedelta(days=int(match.group(1)) * days)
            except OverflowError:
                return None
            return (
                step if step > timedelta(0) else None
            )  # "every 0 days" never advances: unknown rule
    return None


def _day(value: Any) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value not in (None, "") else None


def preview_due_dates(
    parent: dict[str, Any], existing: Sequence[Sequence[Any]], until: date
) -> list[tuple[int, date]]:
    """(occurrence, due date) pairs the API would create; ``existing`` is (occurrence, due_date, completed_at)."""
    step = recurrence_step(parent.get("recurrence_rule"))
    if step is None:
        return []
    if existing:
        occurrence, due, completed = existing[-1]
        anchor = _day(due) or _day(completed) or _day(parent.get("due_date"))
        next_due = anchor + step if anchor else until
    else:
        occurrence = 0
        parent_due = _day(parent.get("due_date"))
        next_due = parent_due + step if parent_due else until
    out = []
    while next_due <= until and len(out) < MAX_PREVIEW:
        occurrence += 1
        out.append((occurrence, next_due))
        try:
            next_due += step
        except OverflowError:
            break
    return out


def list_tasks(http: Http) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = None
    while True:
        page = http.call(
            "GET",
            "/v1/tasks",
            params={"include_archived": "true", "limit": PAGE, "after": after},
        )["items"]
        items.extend(page)
        if len(page) < PAGE:
            return items
        after = page[-1]["number"]


def recurring_parents(tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        t
        for t in tasks
        if t.get("recurrence_rule")
        and t.get("archived_at") is None
        and t.get("status") != "cancelled"
        and not t.get("is_recurring_instance")
    ]


def _existing(
    tasks: Sequence[dict[str, Any]], parent: dict[str, Any]
) -> list[tuple[int, Any, Any]]:
    rows = [
        (t["recurrence_occurrence"], t.get("due_date"), t.get("completed_at"))
        for t in tasks
        if t.get("is_recurring_instance")
        and t.get("parent_recurring_id") == parent["id"]
        and t.get("recurrence_occurrence") is not None
    ]
    return sorted(rows, key=lambda row: row[0])


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": f"recurring.{event}", **fields}), file=sys.stderr)


def run(http: Http, until: date, *, dry_run: bool) -> dict[str, int]:
    counts = {"parents": 0, "generated": 0, "unknown_rule": 0, "errors": 0}
    tasks = list_tasks(http)
    for parent in recurring_parents(tasks):
        counts["parents"] += 1
        if recurrence_step(parent["recurrence_rule"]) is None:
            counts["unknown_rule"] += 1
            _log(
                "unknown_rule",
                task=parent["number"],
                rule=str(parent["recurrence_rule"])[:60],
            )
            continue
        if dry_run:
            due = preview_due_dates(parent, _existing(tasks, parent), until)
            counts["generated"] += len(due)
            for occurrence, day in due:
                _log(
                    "would_generate",
                    task=parent["number"],
                    occurrence=occurrence,
                    due=day.isoformat(),
                )
            continue
        try:
            result = http.call(
                "POST",
                f"/v1/tasks/{parent['id']}/occurrences/generate",
                {"until": until.isoformat()},
            )
        except ApiError as exc:  # one broken parent never blocks the rest
            if not counts["errors"]:  # one Sentry event per run, not one per parent
                capture(exc)
            counts["errors"] += 1
            _log("parent_failed", task=parent["number"], detail=str(exc))
            continue
        counts["generated"] += len(result["created"])
        for created in result["created"]:
            _log(
                "generated",
                task=parent["number"],
                created=created["number"],
                due=str(created.get("due_date")),
            )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-recurring", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be generated; write nothing",
    )
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        help="generate up to this date (default: today, Asia/Bangkok)",
    )
    args = parser.parse_args(argv)
    env = os.environ
    url, token = (
        env.get("EMPIRE_TASKS_API_URL", "").strip(),
        env.get("RECURRING_GEN_TOKEN", "").strip(),
    )
    missing = [
        k
        for k, v in (("EMPIRE_TASKS_API_URL", url), ("RECURRING_GEN_TOKEN", token))
        if not v
    ]
    if missing:
        print(
            json.dumps(
                {
                    "event": "recurring.run",
                    "error": "missing_config",
                    "missing": missing,
                }
            )
        )
        return 2
    if bad_value(url) or bad_value(token):  # fixed message: never echo either value
        print(json.dumps({"event": "recurring.run", "error": "invalid_config"}))
        return 2
    until = args.until or datetime.now(BANGKOK).date()
    try:
        counts = run(Http(url, token), until, dry_run=args.dry_run)
    except RequestError as exc:
        capture(exc)
        print(json.dumps({"event": "recurring.run", "error": "request_failed"}))
        return 1
    except (ApiError, OSError, urllib.error.URLError) as exc:
        capture(exc)
        print(
            json.dumps(
                {
                    "event": "recurring.run",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:200],
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "event": "recurring.run",
                "dry_run": args.dry_run,
                "until": until.isoformat(),
                **counts,
            }
        )
    )
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
