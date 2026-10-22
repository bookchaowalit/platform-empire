"""Backlog sync: source mapping, Markdown parsing, and idempotent upserts against the real API."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from test_tasks_api import client, keys, token, world  # noqa: F401  (fixtures)

from empire_platform import backlog_sync as sync

OWNER_REPO = "bookchaowalit-website/productivity-tracking-web"


def issue(number: int, **extra) -> dict:
    return {
        "repository_url": f"https://api.github.com/repos/{OWNER_REPO}",
        "number": number,
        "title": f"Issue {number}",
        "html_url": f"https://github.com/{OWNER_REPO}/issues/{number}",
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "autopilot"}, {"name": "autopilot:todo"}, {"name": "P1"}],
        "author_association": "OWNER",
        **extra,
    }


SENTRY = {
    "id": "7798457520",
    "shortId": "ATLAS-1",
    "project": {"slug": "devtools-awesome-atlas"},
    "title": "ValueError: no repo for someone@example.com",
    "metadata": {"type": "ValueError", "value": "no repo for someone@example.com"},
    "culprit": "awesome_api.sync in _capture",
    "level": "error",
    "permalink": "https://personal-yu0.sentry.io/issues/7798457520/",
    "firstSeen": "2026-10-18T08:02:46Z",
    "substatus": "ongoing",
}

REVIEW = """---
status: current
---
# PRJ-042 — Outcome Review 1 (T+30)

| Field | Value |
|---|---|
| Review ID | OR-007 |
| Project / product ID | PRJ-042 |
| Status | decided |

## Decisions and actions

| # | Decision | Type | Owner | Due | Tracked in |
|---|---|---|---|---|---|
| 1 | Simplify sign-up step 2 | backlog item | Owner | 2026-11-30 | |
| 2 | Test a shorter onboarding | experiment | Owner | | EXP-003 |
| 3 | Keep the pricing page | persevere | Owner | | |
| 4 | [e.g. Simplify KYC step 2] | backlog item | [Owner] | [YYYY-MM-DD] | |
"""

INCIDENT = """# Incident Review

| Field | Value |
|---|---|
| Incident ID/severity | INC-03 / SEV2 |

## Review

| Action | Type/prevention-detection-recovery | Owner | Due date | Status/evidence |
|---|---|---|---|---|
| Add a timeout alert | detection | Owner | 2026-11-05 | open |
| Retry the README fetch | recovery | Owner | | done in abc123 |
|  |  |  |  |  |
"""


# --- mapping and parsing ------------------------------------------------------------


def test_github_issue_maps_state_labels_and_priority():
    item = sync.github_item(issue(9))
    assert item.key == f"github/{OWNER_REPO}#9"
    assert item.body["status"] == "todo"
    assert item.body["priority"] == 2  # P1
    assert item.body["project"] == "productivity-tracking-web"
    assert item.body["tags"] == ["autopilot", "autopilot:todo", "p1", "source:github"]
    doing = issue(9, labels=[{"name": "autopilot:doing"}])
    assert sync.github_item(doing).body["status"] == "in_progress"
    assert "priority" not in sync.github_item(doing).body
    done = sync.github_item(issue(9, state="closed", state_reason="completed"))
    assert (done.body["status"], done.archive_reason) == ("done", None)
    dropped = sync.github_item(issue(9, state="closed", state_reason="not_planned"))
    assert dropped.body["status"] == "cancelled"
    assert dropped.archive_reason


def test_sentry_title_never_falls_back_to_the_message_and_cleans_the_location():
    bare = {**SENTRY, "metadata": {}, "culprit": "/u/someone@example.com?token=abc"}
    title = sync.sentry_item(bare).body["title"]
    assert "@" not in title
    assert "token=" not in title
    assert ": error at" in title


def test_sentry_item_never_copies_the_exception_message():
    item = sync.sentry_item(SENTRY)
    assert item.key == "sentry/devtools-awesome-atlas/7798457520"
    assert "example.com" not in json.dumps(item.body)
    assert item.body["title"].startswith("[sentry] devtools-awesome-atlas: ValueError")
    assert "status" not in item.body  # tasks-api keeps a done task done
    regressed = sync.sentry_item({**SENTRY, "substatus": "regressed"})
    assert regressed.body["status"] == "todo"


def test_outcome_review_rows_become_tasks_and_placeholders_do_not():
    url = f"https://github.com/{OWNER_REPO}/blob/main/docs/or-007.md"
    items = sync.outcome_review_items(REVIEW, url, OWNER_REPO)
    assert [i.key for i in items] == [
        "outcome-review/OR-007#1",
        "outcome-review/OR-007#2",
    ]
    first, second = items
    assert first.body["due_date"] == "2026-11-30"
    assert first.body["tags"] == ["source:outcome-review", "type:feature"]
    assert second.body["tags"] == ["source:outcome-review", "type:experiment"]
    assert "PRJ-042" in first.body["description"]
    draft = REVIEW.replace("| Status | decided |", "| Status | reviewed |")
    assert sync.outcome_review_items(draft, url, OWNER_REPO) == []


def test_incident_actions_become_tasks():
    items = sync.incident_items(INCIDENT, "https://example.test/inc-03", OWNER_REPO)
    assert [i.key for i in items] == ["incident/INC-03#1", "incident/INC-03#2"]
    assert items[0].body["tags"] == ["source:incident", "type:chore"]
    assert items[0].body["due_date"] == "2026-11-05"
    assert items[1].body["status"] == "done"
    assert items[1].body["tags"] == ["source:incident", "type:fix"]


def test_templates_are_never_read_as_records():
    assert sync._is_template("templates/sdlc/outcome-review.md", REVIEW)
    assert sync._is_template("docs/x.md", "---\nstatus: template\n---\n")
    assert not sync._is_template("docs/reviews/or-007.md", REVIEW)


# --- sources over HTTP (mocked; never the live services) -------------------------------


def mock(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )


def test_github_search_skips_pull_requests_and_pages():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        page = int(request.url.params["page"])
        if "is:open" in request.url.params["q"] and page == 1:
            items = [issue(n) for n in range(100)]
            items[0]["pull_request"] = {}
            return httpx.Response(200, json={"items": items})
        if page == 2:
            return httpx.Response(
                200,
                json={
                    "items": [
                        issue(500),
                        issue(501, author_association="NONE"),
                        issue(
                            502,
                            author_association="CONTRIBUTOR",
                            user={"login": "github-actions[bot]"},
                        ),
                        issue(  # the same bot in a repo that is not an autopilot repo
                            503,
                            author_association="CONTRIBUTOR",
                            user={"login": "github-actions[bot]"},
                            repository_url="https://api.github.com/repos/bookchaowalit/other",
                        ),
                    ]
                },
            )
        return httpx.Response(200, json={"items": []})

    found = list(sync.github_issues(mock(handler), ["a", "b"], [OWNER_REPO]))
    # 99 + 2 from page 2 (one by the autopilot repo's workflow bot); the PR, the
    # outside author's issue and the bot outside an autopilot repo are dropped
    assert len(found) == 101
    assert all("user:a user:b" in q and "label:autopilot,backlog" in q for q in queries)


def test_sentry_follows_the_cursor_only_while_results_remain():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            nxt = '<https://api.test/next>; rel="next"; results="true"; cursor="1"'
            return httpx.Response(200, json=[SENTRY], headers={"link": nxt})
        nxt = '<https://api.test/end>; rel="next"; results="false"; cursor="2"'
        return httpx.Response(200, json=[SENTRY], headers={"link": nxt})

    assert len(list(sync.sentry_issues(mock(handler), "personal-yu0"))) == 2
    assert len(calls) == 2


def test_a_failing_source_is_reported_and_the_others_still_run(capsys):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    def sentry_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[SENTRY])

    report = sync.Report()
    items = sync.collect(
        ["github", "sentry"], mock(down), mock(sentry_ok), ["a"], "o", report
    )
    assert [i.source for i in items] == ["sentry"]
    assert report.counts["errors"] == 1
    assert '"status": 502' in capsys.readouterr().out


def test_missing_configuration_exits_2(capsys):
    assert sync.main([], env={}) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "missing_config"
    assert "BACKLOG_SYNC_TOKEN" in out["missing"]
    assert (
        sync.main(["--dry-run", "--sources", "sentry"], env={"SENTRY_AUTH_TOKEN": " "})
        == 2
    )


# --- against the real tasks-api (embedded PostgreSQL) ---------------------------------


@pytest.fixture
def api(client, token, world):
    """A real httpx.Client (what production uses) whose transport is the tasks-api app."""
    owner = token(world.owner, world.alpha)
    app = TestClient(client.app, headers=owner)

    def forward(request: httpx.Request) -> httpx.Response:
        response = app.request(
            request.method,
            request.url.raw_path.decode(),  # keeps the percent-encoding the sync sent
            content=request.content,
            headers={
                "Content-Type": "application/json",
                **{k: v for k, v in request.headers.items() if k == "if-match"},
            },
        )
        return httpx.Response(response.status_code, content=response.content)

    http = httpx.Client(
        base_url="http://tasks.test", transport=httpx.MockTransport(forward)
    )
    return sync.TasksApi(http), owner


def unique(item: sync.Item) -> sync.Item:
    """Same item under a key no other test uses."""
    return sync.Item(
        item.source,
        f"{item.external_id}-{uuid.uuid4().hex[:6]}",
        item.body,
        item.archive_reason,
    )


def test_second_run_writes_nothing_and_dry_run_never_writes(api, client):
    tasks, _owner = api
    items = [unique(sync.github_item(issue(9))), unique(sync.sentry_item(SENTRY))]
    dry = sync.Report()
    sync.apply(items, tasks, dry, dry_run=True)
    assert dry.counts["would_create"] == 2
    assert all(tasks.get(i) is None for i in items)

    first, second = sync.Report(), sync.Report()
    sync.apply(items, tasks, first, dry_run=False)
    sync.apply(items, tasks, second, dry_run=False)
    assert first.counts["created"] == 2
    assert (
        second.counts["unchanged"],
        second.counts["updated"],
        second.counts["created"],
    ) == (2, 0, 0)
    stored = tasks.get(items[0])
    assert stored is not None
    assert stored["external_id"].endswith(items[0].external_id[-6:])
    assert stored["external_url"] == items[0].body["external_url"]


def test_source_edits_update_only_sync_fields_and_keep_owner_priority(api, client):
    tasks, owner = api
    item = unique(sync.github_item(issue(10)))
    sync.apply([item], tasks, sync.Report(), dry_run=False)
    task = tasks.get(item)
    assert task is not None
    assert task["priority"] == 2
    client.patch(
        f"/v1/tasks/{task['id']}", json={"priority": 1}, headers=owner
    ).raise_for_status()

    renamed = sync.Item(
        item.source, item.external_id, {**item.body, "title": "Renamed upstream"}
    )
    report = sync.Report()
    sync.apply([renamed], tasks, report, dry_run=False)
    after = tasks.get(item)
    assert report.counts["updated"] == 1
    assert after is not None
    assert (after["title"], after["priority"]) == (
        "Renamed upstream",
        1,
    )


def test_issue_closed_as_not_planned_is_cancelled_and_archived(api):
    tasks, _ = api
    open_item = unique(sync.github_item(issue(11)))
    sync.apply([open_item], tasks, sync.Report(), dry_run=False)
    closed = sync.github_item(issue(11, state="closed", state_reason="not_planned"))
    closed = sync.Item(
        closed.source, open_item.external_id, closed.body, closed.archive_reason
    )
    report = sync.Report()
    sync.apply(
        [closed, closed], tasks, report, dry_run=False
    )  # the duplicate is skipped
    task = tasks.get(closed)
    assert task is not None
    assert task["status"] == "cancelled"
    assert task["archived_at"]
    assert (report.counts["updated"], report.counts["archived"]) == (1, 1)


def test_sentry_regression_reopens_a_done_task_but_ongoing_does_not(api, client):
    tasks, owner = api
    item = unique(sync.sentry_item(SENTRY))
    sync.apply([item], tasks, sync.Report(), dry_run=False)
    task = tasks.get(item)
    assert task is not None
    client.post(f"/v1/tasks/{task['id']}/complete", headers=owner).raise_for_status()
    sync.apply([item], tasks, quiet := sync.Report(), dry_run=False)
    assert quiet.counts["unchanged"] == 1
    regressed = sync.Item(
        item.source, item.external_id, {**item.body, "status": "todo"}
    )
    sync.apply([regressed], tasks, sync.Report(), dry_run=False)
    reopened = tasks.get(item)
    assert reopened is not None
    assert reopened["status"] == "todo"


def test_an_api_error_on_one_item_does_not_stop_the_rest(api):
    tasks, _ = api
    bad = sync.Item(
        "github", "x#1", {"title": "", "status": "todo"}
    )  # empty title -> 422
    good = unique(sync.github_item(issue(12)))
    report = sync.Report()
    sync.apply([bad, good], tasks, report, dry_run=False)
    assert (report.counts["errors"], report.counts["created"]) == (1, 1)


def test_a_task_edited_after_the_read_is_not_overwritten(api, client):
    tasks, owner = api
    item = unique(sync.github_item(issue(13)))
    sync.apply([item], tasks, sync.Report(), dry_run=False)
    task = tasks.get(item)
    assert task is not None
    client.patch(
        f"/v1/tasks/{task['id']}", json={"title": "Owner's edit"}, headers=owner
    ).raise_for_status()
    with pytest.raises(httpx.HTTPStatusError) as stale:
        tasks.put(item, {"title": "From GitHub"}, task["version"])
    assert stale.value.response.status_code == 412
    kept = tasks.get(item)
    assert kept is not None
    assert kept["title"] == "Owner's edit"
