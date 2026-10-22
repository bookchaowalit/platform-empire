"""Operations the SQLite writers need, and that retries never duplicate."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from test_tasks_api import client, create, keys, superuser, token, world  # noqa: F401  (fixtures)


def events_for(cluster, urn: str) -> list[str]:
    return [
        row[0]
        for row in superuser(
            cluster,
            "SELECT type FROM tasks.outbox WHERE subject = %s ORDER BY occurred_at, id",
            (urn,),
        )
    ]


def test_external_upsert_creates_once_then_updates_or_does_nothing(
    client, token, world, cluster
):
    owner = token(world.owner, world.alpha)
    key = f"/v1/tasks/external/task-sync/LT-{uuid.uuid4().hex[:6]}"
    first = client.put(key, json={"title": "Ticket", "priority": 2}, headers=owner)
    retry = client.put(key, json={"title": "Ticket", "priority": 2}, headers=owner)
    changed = client.put(key, json={"title": "Ticket", "priority": 1}, headers=owner)
    assert (first.status_code, first.json()["outcome"]) == (201, "created")
    assert (retry.status_code, retry.json()["outcome"]) == (200, "unchanged")
    assert (changed.status_code, changed.json()["outcome"]) == (200, "updated")
    task = first.json()["task"]
    assert retry.json()["task"]["id"] == changed.json()["task"]["id"] == task["id"]
    assert task["external_source"] == "task-sync"
    assert events_for(cluster, task["urn"]) == [
        "tasks.task.created",
        "tasks.task.updated",
    ]


def test_concurrent_external_upserts_create_one_task(client, token, world, cluster):
    owner = token(world.owner, world.alpha)
    external_id = f"race-{uuid.uuid4().hex[:6]}"
    results: list[int] = []

    def put() -> None:
        results.append(
            client.put(
                f"/v1/tasks/external/gmail/{external_id}",
                json={"title": "Reply"},
                headers=owner,
            ).status_code
        )

    threads = [threading.Thread(target=put) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results).count(201) == 1
    assert all(code in (200, 201) for code in results)
    count = superuser(
        cluster,
        "SELECT count(*) FROM tasks.tasks WHERE external_source = 'gmail' AND external_id = %s",
        (external_id,),
    )[0][0]
    assert count == 1


def test_same_external_key_in_another_workspace_is_separate(client, token, world):
    path = f"/v1/tasks/external/engagements/E-{uuid.uuid4().hex[:6]}"
    alpha = client.put(
        path, json={"title": "Alpha"}, headers=token(world.owner, world.alpha)
    ).json()
    beta = client.put(
        path, json={"title": "Beta"}, headers=token(world.outsider, world.beta)
    ).json()
    assert alpha["outcome"] == beta["outcome"] == "created"
    assert alpha["task"]["id"] != beta["task"]["id"]


@pytest.mark.parametrize(
    "path",
    [
        "{repo}%2342",  # the slash as-is, the '#' encoded (the policy's form)
        "{repo_encoded}%2342",  # every reserved character encoded
    ],
)
def test_github_issue_key_with_slash_and_hash_upserts_one_task(
    client, token, world, path
):
    """Policy key ``github/<owner>/<repo>#<n>`` (TASK-SOURCE-OF-TRUTH rule 2)."""
    owner = token(world.owner, world.alpha)
    repo = f"bookchaowalit/repo-{uuid.uuid4().hex[:6]}"
    url = "/v1/tasks/external/github/" + path.format(
        repo=repo, repo_encoded=repo.replace("/", "%2F")
    )
    first = client.put(url, json={"title": "Issue 42"}, headers=owner)
    retry = client.put(url, json={"title": "Issue 42"}, headers=owner)
    assert (first.status_code, first.json()["outcome"]) == (201, "created")
    assert retry.json()["outcome"] == "unchanged"
    assert first.json()["task"]["external_id"] == f"{repo}#42"
    found = client.get(url, headers=owner)
    assert found.status_code == 200
    assert found.json()["id"] == first.json()["task"]["id"]


def test_reminder_delivery_is_recorded_once(client, token, world, cluster):
    owner = token(world.owner, world.alpha)
    reminder_at = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    task = create(
        client,
        owner,
        title="Stretch",
        reminder_at=reminder_at.isoformat(),
        reminder_channels="line",
    )
    body = {"reminder_at": reminder_at.isoformat(), "channel": "line"}
    first = client.post(
        f"/v1/tasks/{task['id']}/reminders/delivered", json=body, headers=owner
    )
    retries = [
        client.post(
            f"/v1/tasks/{task['id']}/reminders/delivered", json=body, headers=owner
        )
        for _ in range(3)
    ]
    assert first.json()["outcome"] == "recorded"
    assert {r.json()["outcome"] for r in retries} == {"already_recorded"}
    assert (
        client.get(f"/v1/tasks/{task['id']}", headers=owner).json()["reminder_sent"]
        is True
    )
    deliveries = superuser(
        cluster,
        "SELECT count(*) FROM tasks.reminder_deliveries WHERE task_id = %s",
        (task["id"],),
    )[0][0]
    assert deliveries == 1
    assert events_for(cluster, task["urn"]).count("tasks.reminder.delivered") == 1


def test_recurring_occurrences_are_generated_once(client, token, world, cluster):
    owner = token(world.owner, world.alpha)
    parent = create(
        client,
        owner,
        title="Weekly long run",
        recurrence_rule="weekly",
        due_date="2026-10-01",
    )
    until = {"until": "2026-10-22"}
    first = client.post(
        f"/v1/tasks/{parent['id']}/occurrences/generate", json=until, headers=owner
    ).json()
    retry = client.post(
        f"/v1/tasks/{parent['id']}/occurrences/generate", json=until, headers=owner
    ).json()
    assert [t["due_date"] for t in first["created"]] == [
        "2026-10-08",
        "2026-10-15",
        "2026-10-22",
    ]
    assert [t["title"] for t in first["created"]][0] == "[R1] Weekly long run"  # noqa: RUF015 - an empty list should fail as IndexError
    assert retry["created"] == []
    assert retry["existing"] == 3
    later = client.post(
        f"/v1/tasks/{parent['id']}/occurrences/generate",
        json={"until": "2026-10-29"},
        headers=owner,
    ).json()
    assert [(t["recurrence_occurrence"], t["due_date"]) for t in later["created"]] == [
        (4, "2026-10-29")
    ]
    instances = superuser(
        cluster,
        "SELECT count(*) FROM tasks.tasks WHERE parent_recurring_id = %s",
        (parent["id"],),
    )[0][0]
    assert instances == 4


def test_generate_requires_a_rule(client, token, world):
    owner = token(world.owner, world.alpha)
    task = create(client, owner, title="One-off")
    response = client.post(
        f"/v1/tasks/{task['id']}/occurrences/generate",
        json={"until": str(date.today())},
        headers=owner,
    )
    assert response.status_code == 422


def test_writer_fields_round_trip_and_archive(client, token, world, cluster):
    owner = token(world.owner, world.alpha)
    parent = create(client, owner, title="Plan race")
    child = create(
        client,
        owner,
        title="Book hotel",
        parent_task_id=parent["id"],
        notes="near start line",
        calendar_event_id="gcal-1",
        estimated_minutes=30,
    )
    assert (child["parent_task_id"], child["notes"], child["calendar_event_id"]) == (
        parent["id"],
        "near start line",
        "gcal-1",
    )
    patched = client.patch(
        f"/v1/tasks/{child['id']}",
        headers=owner,
        json={
            "calendar_event_id": "gcal-2",
            "actual_minutes": 25,
            "started_at": "2026-10-09T08:00:00+07:00",
        },
    ).json()
    assert (patched["calendar_event_id"], patched["actual_minutes"]) == ("gcal-2", 25)
    archived = client.post(
        f"/v1/tasks/{child['id']}/archive", json={"reason": "booked"}, headers=owner
    ).json()
    assert archived["archived_at"] is not None
    assert archived["archive_reason"] == "booked"
    assert child["id"] not in [
        t["id"] for t in client.get("/v1/tasks", headers=owner).json()["items"]
    ]
    assert events_for(cluster, child["urn"])[-1] == "tasks.task.archived"


def test_parent_from_another_workspace_is_rejected(client, token, world):
    beta_task = create(client, token(world.outsider, world.beta), title="Beta parent")
    response = client.post(
        "/v1/tasks",
        json={"title": "Child", "parent_task_id": beta_task["id"]},
        headers=token(world.owner, world.alpha),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "reference_not_found"


def test_generate_rejects_a_zero_interval_instead_of_looping(client, token, world):
    owner = token(world.owner, world.alpha)
    for rule in ("every 0 days", "every 0 weeks", "every 0 months"):
        parent = create(
            client,
            owner,
            title=f"Broken {rule}",
            recurrence_rule=rule,
            due_date="2026-10-01",
        )
        response = client.post(
            f"/v1/tasks/{parent['id']}/occurrences/generate",
            json={"until": "2026-12-31"},
            headers=owner,
        )
        assert response.status_code == 422, rule


def test_generate_is_capped_per_call_and_a_retry_continues(
    client, token, world, cluster
):
    from tasks_api.app import MAX_OCCURRENCES_PER_CALL

    owner = token(world.owner, world.alpha)
    parent = create(
        client,
        owner,
        title="Daily forever",
        recurrence_rule="daily",
        due_date="2020-01-01",
    )
    body = {"until": "2099-01-01"}
    first = client.post(
        f"/v1/tasks/{parent['id']}/occurrences/generate", json=body, headers=owner
    ).json()
    assert len(first["created"]) == MAX_OCCURRENCES_PER_CALL
    second = client.post(
        f"/v1/tasks/{parent['id']}/occurrences/generate", json=body, headers=owner
    ).json()
    assert len(second["created"]) == MAX_OCCURRENCES_PER_CALL
    assert second["existing"] == MAX_OCCURRENCES_PER_CALL
    assert (
        first["created"][-1]["recurrence_occurrence"] + 1
        == second["created"][0]["recurrence_occurrence"]
    )
