"""Feed the single backlog: GitHub issues, Sentry errors, outcome reviews and incidents -> tasks-api.

Owner decision D2 (2026-10-23) makes tasks-api the only backlog. This job turns every
signal that needs work into exactly one task, keyed by its external identity
(architecture-docs ``docs/lifecycle/OUTCOME-REVIEW-TO-BACKLOG.md``):

=================  =====================================  ==========================
Source             Key (``PUT /v1/tasks/external/...``)   What is read
=================  =====================================  ==========================
GitHub issue       ``github/<owner>/<repo>#<n>``          label ``autopilot`` or ``backlog``
Sentry issue       ``sentry/<project>/<issue id>``        unresolved, level error/fatal
Outcome review     ``outcome-review/<OR-id>#<row>``       ``decided`` reviews, action rows
Incident review    ``incident/<INC-id>#<row>``            corrective-action rows
=================  =====================================  ==========================

Field ownership follows TASK-SOURCE-OF-TRUTH: the source owns title, link, labels and
(for GitHub) state; tasks-api owns priority, due date and assignee, so those are sent
only when the task is created. Each item is read first and written only when a
sync-owned field differs, so a re-run writes nothing and emits no outbox events.

    empire-backlog-sync [--dry-run] [--sources github,sentry,files]

``--dry-run`` reads the sources (and tasks-api when a token is set) and prints what it
would create or update; it writes nothing. Without a tasks-api token, dry-run prints
every item as ``would_upsert``. One failing source or item is logged and skipped; the
exit code is 1 when anything failed, 2 when configuration is missing.

Environment (nothing secret is printed):
    BACKLOG_SYNC_GITHUB_TOKEN   read-only issues + contents + code search on the owners
    BACKLOG_SYNC_GITHUB_OWNERS  comma list (default: bookchaowalit)
    SENTRY_AUTH_TOKEN, SENTRY_ORG (default personal-yu0)
    EMPIRE_TASKS_API_URL, BACKLOG_SYNC_TOKEN   (writes; optional in --dry-run)
    MONITOR_URL, HEARTBEAT_TOKEN               optional heartbeat ``backlog-sync``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from ._sentry import capture

GITHUB_API = "https://api.github.com"
SENTRY_API = "https://sentry.io/api/0"
SOURCES = ("github", "sentry", "files")
# Labels P0..P3 (runbook) -> tasks-api priority 1..4 (1 is the highest; 3 is the default).
PRIORITY = {"p0": 1, "p1": 2, "p2": 3, "p3": 4}
# Outcome-review action types (template "Decisions and actions") -> runbook type label.
REVIEW_TYPES = {
    "backlog item": "feature",
    "experiment": "experiment",
    "discovery question": "research",
    "kpi definition change": "chore",
    "retire feature": "chore",
    "change request": "chore",
    "roadmap change": "chore",
}
# Only people with write access can label, but anyone can open an issue and edit its
# title or close it. Issues by other authors are left out (owner moves them by hand).
TRUSTED_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Only a workflow in the repository itself can post as this bot (autopilot proposals).
TRUSTED_BOTS = {"github-actions[bot]"}
SAFE_LOCATION = re.compile(r"[^\w.:/<>\- ]")
SYNC_FIELDS = ("title", "status", "external_url", "tags", "project", "description")
CREATE_ONLY = ("priority", "due_date")
MAX_TITLE = 500


@dataclass(frozen=True)
class Item:
    """One task the backlog should hold."""

    source: str
    external_id: str
    body: dict[str, Any]
    archive_reason: str | None = None  # archive the task after the upsert (not planned)

    @property
    def key(self) -> str:
        return f"{self.source}/{self.external_id}"


@dataclass
class Report:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "archived": 0,
            "would_create": 0,
            "would_update": 0,
            "would_upsert": 0,
            "errors": 0,
        }
    )

    def log(self, event: str, **fields: Any) -> None:
        counter = "errors" if event == "error" else event
        if counter in self.counts:
            self.counts[counter] += 1
        print(json.dumps({"event": f"backlog.{event}", **fields}, ensure_ascii=False))


def _title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1] + "…"


def _tags(*groups: Iterable[str]) -> list[str]:
    return sorted({t.strip().lower() for g in groups for t in g if t and t.strip()})


def _priority(labels: Iterable[str]) -> int | None:
    found = [PRIORITY[x.lower()] for x in labels if x.lower() in PRIORITY]
    return min(found) if found else None


# --- GitHub issues ---------------------------------------------------------------


def github_item(issue: Mapping[str, Any]) -> Item:
    repo = issue["repository_url"].split("/repos/", 1)[1]
    labels = [label["name"] for label in issue.get("labels", [])]
    lowered = {x.lower() for x in labels}
    archive = None
    if issue["state"] == "closed":
        not_planned = issue.get("state_reason") == "not_planned"
        status = "cancelled" if not_planned else "done"
        archive = "GitHub issue closed as not planned" if not_planned else None
    elif "autopilot:doing" in lowered:
        status = "in_progress"
    elif "autopilot:blocked" in lowered:
        status = "blocked"
    else:
        status = "todo"
    body: dict[str, Any] = {
        "title": _title(f"{repo.split('/')[1]}#{issue['number']}: {issue['title']}"),
        "status": status,
        "external_url": issue["html_url"],
        "project": repo.split("/")[1],
        "tags": _tags(["source:github"], labels),
    }
    if (priority := _priority(labels)) is not None:
        body["priority"] = priority
    return Item("github", f"{repo}#{issue['number']}", body, archive)


def github_issues(
    gh: httpx.Client, owners: Iterable[str]
) -> Iterator[Mapping[str, Any]]:
    """Open issues, and issues closed in the last 30 days, labelled autopilot or backlog."""
    users = " ".join(f"user:{o}" for o in owners)
    since = date.fromordinal(date.today().toordinal() - 30).isoformat()
    for state in (
        "is:open",
        f"is:closed closed:>={since}",
    ):
        query = f"is:issue {state} label:autopilot,backlog {users}"
        for page in range(1, 11):  # the search API returns at most 1,000 results
            response = gh.get(
                "/search/issues", params={"q": query, "per_page": 100, "page": page}
            )
            response.raise_for_status()
            items = response.json()["items"]
            yield from (
                i
                for i in items
                if "pull_request" not in i
                and (
                    i.get("author_association") in TRUSTED_AUTHORS
                    or (i.get("user") or {}).get("login") in TRUSTED_BOTS
                )
            )
            if len(items) < 100:
                break


# --- Sentry -------------------------------------------------------------------------


def sentry_item(issue: Mapping[str, Any]) -> Item:
    project = issue["project"]["slug"]
    kind = SAFE_LOCATION.sub("", (issue.get("metadata") or {}).get("type") or "")[:80]
    kind = kind or "error"  # never the title: it holds the message
    where = SAFE_LOCATION.sub("", (issue.get("culprit") or "").split("?")[0])[:120]
    # Type and location only: an exception message can carry personal data (runbook rule 5).
    body: dict[str, Any] = {
        "title": _title(
            f"[sentry] {project}: {kind}" + (f" at {where}" if where else "")
        ),
        "external_url": issue["permalink"],
        "project": project,
        "tags": _tags(["source:sentry", "type:fix", f"level:{issue['level']}"]),
        "description": (
            f"Sentry {issue['shortId']}, first seen {str(issue['firstSeen'])[:10]}."
            " Fix with a failing test first (ADR-0004), then resolve it in the release."
        ),
        "priority": 2 if issue["level"] == "fatal" else 3,
    }
    if issue.get("substatus") == "regressed":
        body["status"] = "todo"  # a regression reopens a done task (runbook rule 2)
    return Item("sentry", f"{project}/{issue['id']}", body)


def sentry_issues(sentry: httpx.Client, org: str) -> Iterator[Mapping[str, Any]]:
    url: str | None = f"/organizations/{org}/issues/"
    params: dict[str, Any] | None = {
        "query": "is:unresolved level:[error,fatal]",
        "statsPeriod": "14d",
        "limit": 100,
    }
    for _ in range(20):
        if url is None:
            return
        response = sentry.get(url, params=params)
        response.raise_for_status()
        yield from response.json()
        nxt = response.links.get("next", {})
        url = nxt.get("url") if nxt.get("results") == "true" else None
        params = None


# --- Outcome reviews and incident reviews (Markdown files in repos) ---------------


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables(markdown: str) -> Iterator[tuple[str, list[str], list[list[str]]]]:
    """(nearest heading, header cells, data rows) for every pipe table."""
    heading, lines = "", markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])
        ):
            header, rows, i = _cells(line), [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            yield heading, header, rows
            continue
        i += 1


def _field(markdown: str, name: str) -> str:
    for _, header, rows in tables(markdown):
        for row in [header, *rows]:
            if len(row) >= 2 and row[0].lower() == name.lower():
                return row[1]
    return ""


def _placeholder(text: str) -> bool:
    return not text or text.startswith("[") or set(text) <= {"-", " "}


def _due(text: str) -> str | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def outcome_review_items(markdown: str, url: str, repo: str) -> list[Item]:
    review = re.search(r"\bOR-\d+\b", _field(markdown, "Review ID"))
    status = _field(markdown, "Status").lower()
    if not review or not status.startswith("decided"):
        return []
    product = _field(markdown, "Project / product ID")
    items = []
    for heading, header, rows in tables(markdown):
        if heading.lower() != "decisions and actions" or header[:1] != ["#"]:
            continue
        for row in rows:
            row += [""] * (6 - len(row))
            number, decision, kind, _owner, due, _tracked = row[:6]
            kind_label = REVIEW_TYPES.get(kind.lower())
            if _placeholder(decision) or not kind_label or not number.isdigit():
                continue
            body: dict[str, Any] = {
                "title": _title(f"{review.group(0)}: {decision}"),
                "external_url": url,
                "project": repo.split("/")[1],
                "tags": _tags(["source:outcome-review", f"type:{kind_label}"]),
                "description": f"Outcome review {review.group(0)}"
                + (f" ({product})" if not _placeholder(product) else "")
                + f", decision {number}: {kind}.",
            }
            if (day := _due(due)) is not None:
                body["due_date"] = day
            items.append(Item("outcome-review", f"{review.group(0)}#{number}", body))
    return items


def incident_items(markdown: str, url: str, repo: str) -> list[Item]:
    incident = re.search(r"\bINC-\d+\b", _field(markdown, "Incident ID/severity"))
    if not incident:
        return []
    items = []
    for _, header, rows in tables(markdown):
        if header[:1] != ["Action"]:
            continue
        for number, row in enumerate(rows, start=1):
            row += [""] * (5 - len(row))
            action, kind, _owner, due, state = row[:5]
            if _placeholder(action):
                continue
            body: dict[str, Any] = {
                "title": _title(f"{incident.group(0)}: {action}"),
                "status": "done" if state.lower().startswith("done") else "todo",
                "external_url": url,
                "project": repo.split("/")[1],
                "tags": _tags(
                    [
                        "source:incident",
                        "type:chore"
                        if "prevent" in kind.lower() or "detect" in kind.lower()
                        else "type:fix",
                    ]
                ),
            }
            if (day := _due(due)) is not None:
                body["due_date"] = day
            items.append(Item("incident", f"{incident.group(0)}#{number}", body))
    return items


FILE_KINDS: dict[str, tuple[str, Callable[[str, str, str], list[Item]]]] = {
    "outcome-review": ('"Review ID" "Decisions and actions"', outcome_review_items),
    "incident": ('"Incident ID/severity"', incident_items),
}


def _is_template(path: str, markdown: str) -> bool:
    head = markdown[:600]
    return "templates/" in path or bool(
        re.search(r"^status:\s*template\s*$", head, re.MULTILINE)
    )


def file_items(
    gh: httpx.Client, owners: Iterable[str], report: Report
) -> Iterator[Item]:
    users = " ".join(f"user:{o}" for o in owners)
    for kind, (terms, parse) in FILE_KINDS.items():
        response = gh.get(
            "/search/code",
            params={"q": f"{terms} extension:md {users}", "per_page": 100},
        )
        response.raise_for_status()
        for hit in response.json()["items"]:
            repo, path = hit["repository"]["full_name"], hit["path"]
            raw = gh.get(
                f"/repos/{repo}/contents/{urllib.parse.quote(path)}",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            if raw.status_code != 200:
                report.log(
                    "error", source=kind, where=f"{repo}/{path}", status=raw.status_code
                )
                continue
            if _is_template(path, raw.text):
                continue
            yield from parse(raw.text, hit["html_url"], repo)


# --- tasks-api ------------------------------------------------------------------------


class TasksApi:
    """The two calls the sync needs; ``client`` carries the base URL and bearer token."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    @staticmethod
    def path(item: Item) -> str:
        return f"/v1/tasks/external/{item.source}/{urllib.parse.quote(item.external_id, safe='/')}"

    def get(self, item: Item) -> dict[str, Any] | None:
        response = self.client.get(self.path(item))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def put(
        self, item: Item, body: Mapping[str, Any], version: Any = None
    ) -> dict[str, Any]:
        """With ``version`` (from the read), a task edited since is not overwritten (412)."""
        headers = {"If-Match": f'"{version}"'} if version is not None else None
        response = self.client.put(self.path(item), json=dict(body), headers=headers)
        response.raise_for_status()
        return response.json()

    def archive(self, task_id: str, reason: str) -> None:
        self.client.post(
            f"/v1/tasks/{task_id}/archive", json={"reason": reason}
        ).raise_for_status()


def changes(item: Item, task: Mapping[str, Any]) -> dict[str, Any]:
    """The sync-owned fields that differ from the stored task."""
    out = {}
    for name in SYNC_FIELDS:
        if name not in item.body:
            continue
        want, have = item.body[name], task.get(name)
        if name == "tags":
            want, have = sorted(want), sorted(have or [])
        if (
            name == "status"
            and item.source == "sentry"
            and have not in ("done", "cancelled")
        ):
            continue  # only a regression of a closed task reopens it
        if want != have:
            out[name] = item.body[name]
    return out


def apply(
    items: Iterable[Item], api: TasksApi | None, report: Report, *, dry_run: bool
) -> None:
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        summary = {"key": item.key, "title": item.body["title"]}
        if api is None:
            report.log("would_upsert", **summary, status=item.body.get("status"))
            continue
        try:
            task = api.get(item)
            if task is None:
                if dry_run:
                    report.log("would_create", **summary)
                    continue
                result = api.put(item, item.body)
                task = result["task"]
                report.log("created", **summary, task=task["number"])
            else:
                diff = changes(item, task)
                if not diff:
                    report.log("unchanged", key=item.key, task=task["number"])
                elif dry_run:
                    report.log("would_update", **summary, fields=sorted(diff))
                else:
                    # TaskCreate needs a title; an unchanged title is not rewritten.
                    body = {"title": item.body["title"], **diff}
                    task = api.put(item, body, task.get("version"))["task"]
                    report.log(
                        "updated",
                        key=item.key,
                        task=task["number"],
                        fields=sorted(diff),
                    )
            if item.archive_reason and task.get("archived_at") is None and not dry_run:
                api.archive(task["id"], item.archive_reason)
                report.log("archived", key=item.key, task=task["number"])
        except httpx.HTTPError as exc:  # one bad item never blocks the rest
            if not report.counts["errors"]:
                capture(exc)
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            report.log("error", key=item.key, kind=type(exc).__name__, status=status)


# --- run -------------------------------------------------------------------------------


def collect(
    sources: Iterable[str],
    gh: httpx.Client | None,
    sentry: httpx.Client | None,
    owners: list[str],
    org: str,
    report: Report,
) -> list[Item]:
    readers: dict[str, Callable[[], Iterable[Item]]] = {
        "github": lambda: (github_item(i) for i in github_issues(gh, owners)),  # type: ignore[arg-type]
        "sentry": lambda: (sentry_item(i) for i in sentry_issues(sentry, org)),  # type: ignore[arg-type]
        "files": lambda: file_items(gh, owners, report),  # type: ignore[arg-type]
    }
    items: list[Item] = []
    for source in sources:
        try:
            found = list(readers[source]())
        except httpx.HTTPError as exc:  # a source that is down is skipped, not fatal
            capture(exc)
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            report.log("error", source=source, kind=type(exc).__name__, status=status)
            continue
        report.log("source", source=source, items=len(found))
        items.extend(found)
    return items


def heartbeat(env: Mapping[str, str], ok: bool, detail: str) -> None:
    url, token = env.get("MONITOR_URL"), env.get("HEARTBEAT_TOKEN")
    if not url or not token:
        return
    try:
        httpx.post(
            f"{url.rstrip('/')}/heartbeat/backlog-sync",
            json={"ok": ok, "detail": detail},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        print(json.dumps({"event": "backlog.heartbeat", "ok": False}))


def _client(base: str, token: str, **headers: str) -> httpx.Client:
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}", **headers},
        timeout=30,
        follow_redirects=False,  # never forward a token
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-backlog-sync", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="read only; print what would change"
    )
    parser.add_argument(
        "--sources", default=",".join(SOURCES), help="comma list of github,sentry,files"
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    sources = [s for s in args.sources.split(",") if s]
    if not set(sources) <= set(SOURCES):
        parser.error(f"unknown source in {args.sources!r}")
    gh_token = env.get("BACKLOG_SYNC_GITHUB_TOKEN", "").strip()
    sentry_token = env.get("SENTRY_AUTH_TOKEN", "").strip()
    url, token = (
        env.get("EMPIRE_TASKS_API_URL", "").strip(),
        env.get("BACKLOG_SYNC_TOKEN", "").strip(),
    )
    need = {
        "BACKLOG_SYNC_GITHUB_TOKEN": gh_token
        if {"github", "files"} & set(sources)
        else "-",
        "SENTRY_AUTH_TOKEN": sentry_token if "sentry" in sources else "-",
        "EMPIRE_TASKS_API_URL": url if not args.dry_run else "-",
        "BACKLOG_SYNC_TOKEN": token if not args.dry_run else "-",
    }
    if missing := [k for k, v in need.items() if not v]:
        print(
            json.dumps(
                {"event": "backlog.run", "error": "missing_config", "missing": missing}
            )
        )
        return 2
    if (
        url
        and urllib.parse.urlsplit(url).scheme != "https"
        and urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "localhost")
    ):
        print(
            json.dumps(
                {"event": "backlog.run", "error": "EMPIRE_TASKS_API_URL must be https"}
            )
        )
        return 2
    owners = [
        o.strip()
        for o in env.get("BACKLOG_SYNC_GITHUB_OWNERS", "bookchaowalit").split(",")
        if o.strip()
    ]
    report = Report()
    gh = (
        _client(GITHUB_API, gh_token, Accept="application/vnd.github+json")
        if gh_token
        else None
    )
    sentry = _client(SENTRY_API, sentry_token) if sentry_token else None
    tasks = (
        _client(url, token, **{"X-Empire-Channel": "workflow"})
        if url and token
        else None
    )
    try:
        items = collect(
            sources, gh, sentry, owners, env.get("SENTRY_ORG", "personal-yu0"), report
        )
        apply(items, TasksApi(tasks) if tasks else None, report, dry_run=args.dry_run)
    finally:
        for client in (gh, sentry, tasks):
            if client is not None:
                client.close()
    ok = report.counts["errors"] == 0
    print(
        json.dumps(
            {"event": "backlog.run", "dry_run": args.dry_run, "ok": ok, **report.counts}
        )
    )
    if not args.dry_run:
        heartbeat(env, ok, f"errors={report.counts['errors']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
