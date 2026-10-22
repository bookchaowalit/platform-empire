"""Daily check: least-privilege monitor role and each rollback signal."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from conftest import create_workspace, use_workspace
from graph_world import apps, bearer, gkeys, gtoken, gworld, run_publisher  # noqa: F401  (fixtures)

from empire_platform import cells
from empire_platform.devtoken import key_pair, mint
from empire_platform.monitor import evaluate, run, snapshot, token_expiries
from empire_platform.permissions import DECISIONS_OWNER


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        result = conn.execute(query, params)
        return result.fetchall() if result.description else None


def test_monitor_role_cannot_read_content_or_write(cluster):
    with cluster.connect("platform_monitor") as conn:
        for query in (
            "SELECT title FROM tasks.tasks",
            "SELECT payload FROM tasks.outbox",
            "SELECT last_error FROM tasks.outbox",
            "SELECT label FROM graph.nodes",
            "UPDATE tasks.settings SET value = 'false'",
            "SELECT resource_id FROM platform.cell_executions",
            "SELECT executed_by FROM platform.cell_executions",
            "SELECT executed_by FROM platform.executions",
            "SELECT action FROM platform.audit_events",
            "UPDATE platform.executions SET status = 'failed'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
            conn.rollback()


def test_snapshot_sees_every_workspace(cluster, apps, gworld, gtoken):
    tasks, _ = apps
    tasks.post(
        "/v1/tasks",
        json={"title": "Counted"},
        headers=bearer(gtoken(gworld.owner, gworld.ws)),
    )
    snap = snapshot(cluster)
    assert str(gworld.ws) in snap["workspaces"]
    assert snap["workspaces"][str(gworld.ws)]["tasks"] >= 1
    assert snap["writes_enabled"] is True


def base(**workspaces):
    return {
        "workspaces": workspaces,
        "outbox": {"oldest_age_seconds": 0, "max_attempts": 0, "graph_lagging": 0},
        "writes_enabled": True,
        "workspace_mismatch_denials_24h": 0,
        "cell_executions": {},
    }


def checks(findings):
    return {(f.check, f.level) for f in findings}


def test_lost_tasks_breach_but_recorded_deletions_do_not():
    now = datetime.now(UTC)
    before = base(w={"tasks": 10, "distinct_numbers": 10, "deleted_events": 0})
    lost = base(w={"tasks": 8, "distinct_numbers": 8, "deleted_events": 1})
    deleted = base(w={"tasks": 9, "distinct_numbers": 9, "deleted_events": 1})
    assert ("tasks_lost", "breach") in checks(
        evaluate(lost, before, None, None, now)[0]
    )
    assert evaluate(deleted, before, None, None, now)[0] == []
    assert ("workspace_missing", "breach") in checks(
        evaluate(base(), before, None, None, now)[0]
    )


def test_outbox_api_and_switch_signals():
    now = datetime.now(UTC)
    snap = base()
    snap["outbox"].update(oldest_age_seconds=20 * 60, max_attempts=4, graph_lagging=2)
    snap["writes_enabled"] = False
    found = checks(evaluate(snap, None, True, None, now)[0])
    assert {
        ("outbox_backlog", "warn"),
        ("outbox_retries", "warn"),
        ("graph_lag", "warn"),
        ("writes_disabled", "warn"),
    } <= found
    snap["outbox"]["oldest_age_seconds"] = 2 * 3600
    assert ("outbox_backlog", "breach") in checks(
        evaluate(snap, None, True, None, now)[0]
    )
    first, since = evaluate(base(), None, False, None, now)
    assert ("api_down", "warn") in checks(first)
    assert since
    later, _ = evaluate(
        base(), None, False, (now - timedelta(minutes=31)).isoformat(), now
    )
    assert ("api_down", "breach") in checks(later)
    _, cleared = evaluate(base(), None, True, since, now)
    assert cleared is None


def test_run_keeps_state_between_days(cluster, tmp_path, apps, gworld, gtoken):
    run_publisher(cluster)
    first = run(cluster, None, tmp_path)
    assert first["level"] in ("ok", "warn")
    # A task vanishing without a delete event is the signal the owner rolls back on.
    tasks, _ = apps
    made = tasks.post(
        "/v1/tasks",
        json={"title": f"Vanishes {uuid.uuid4().hex[:6]}"},
        headers=bearer(gtoken(gworld.owner, gworld.ws)),
    ).json()
    run_publisher(cluster)
    run(cluster, None, tmp_path)
    superuser(cluster, "DELETE FROM tasks.tasks WHERE id = %s", (made["id"],))
    after = run(cluster, None, tmp_path)
    assert after["level"] == "breach"
    assert any(
        f["check"] == "tasks_lost" and f["workspace"] == str(gworld.ws)
        for f in after["findings"]
    )


def test_cell_execution_signals():
    now = datetime.now(UTC)
    snap = base()
    snap["cell_executions"] = {"decisions": {"running_warn": 2, "running_breach": 0}}
    assert checks(evaluate(snap, None, None, None, now)[0]) == {
        ("cell_executions_stuck", "warn")
    }
    snap["cell_executions"]["decisions"]["running_breach"] = 1
    assert checks(evaluate(snap, None, None, None, now)[0]) == {
        ("cell_executions_stuck", "breach")
    }
    snap["cell_executions"] = {"decisions": {"running_warn": 0, "running_breach": 0}}
    assert evaluate(snap, None, None, None, now)[0] == []


def test_snapshot_counts_stuck_cell_executions_and_reconcile_clears_them(cluster):
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"monitor-cells-{uuid.uuid4().hex[:8]}", list(DECISIONS_OWNER)
        )
        use_workspace(conn, ws.id)
        execution = conn.execute(
            "SELECT execution_id FROM platform.begin_cell_execution("
            "%s, %s, %s, NULL, NULL, 'api', NULL, NULL, 'decisions', 'decisions.decision:write', 'decision', 'd-1',"
            " NULL)",
            (ws.owner, ws.owner, ws.owner),
        ).fetchone()[0]
        conn.commit()

    def counts():
        return snapshot(cluster)["cell_executions"].get(
            "decisions", {"running_warn": 0, "running_breach": 0}
        )

    before = counts()
    try:
        superuser(
            cluster,
            "UPDATE platform.executions SET started_at = now() - interval '25 hours' WHERE id = %s",
            (execution,),
        )
        after = counts()
        assert after["running_warn"] == before["running_warn"] + 1
        assert after["running_breach"] == before["running_breach"] + 1
    finally:
        assert (
            cells.reconcile(cluster, ws.id, "decisions", execution, "failed")
            == "finished"
        )
    assert counts() == before


def write_token(directory, name, ttl, private_pem):
    token = mint(
        private_pem, sub=uuid.uuid4(), ws=uuid.uuid4(), ttl=int(ttl.total_seconds())
    )
    (directory / f"{name}.jwt").write_text(token)
    return token


def test_token_expiry_reads_only_exp_and_skips_short_lived(tmp_path):
    private_pem, _ = key_pair(tmp_path / "keys")
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    write_token(tokens, "fresh", timedelta(days=30), private_pem)
    write_token(tokens, "soon", timedelta(days=30), private_pem)
    write_token(tokens, "owner-cli", timedelta(hours=1), private_pem)
    (tokens / "broken.jwt").write_text("not-a-token")
    for name, exp in (("infinite", "1e999"), ("far", str(10**20)), ("text", '"soon"')):
        claims = (
            base64.urlsafe_b64encode(f'{{"exp": {exp}}}'.encode()).decode().rstrip("=")
        )
        (tokens / f"{name}.jwt").write_text(f"e30.{claims}.sig")
    # Re-mint "soon" as a 30-day token issued 25 days ago.
    header, payload, signature = (tokens / "soon.jwt").read_text().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["iat"] -= 25 * 86400
    claims["exp"] -= 25 * 86400
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    (tokens / "soon.jwt").write_text(f"{header}.{forged}.{signature}")

    expiries = token_expiries(tokens)
    unreadable = {"broken", "infinite", "far", "text"}
    assert set(expiries) == {"fresh", "soon"} | unreadable
    assert all(expiries[name] is None for name in unreadable)
    assert isinstance(expiries["fresh"], int)
    assert isinstance(expiries["soon"], int)
    assert token_expiries(tmp_path / "missing") == {}
    assert token_expiries(None) == {}

    now = datetime.now(UTC)
    snap = base()
    snap["tokens"] = expiries
    found = evaluate(snap, None, None, None, now)[0]
    assert {(f.check, f.level) for f in found} == {
        ("token_expiring", "warn"),
        ("token_unreadable", "warn"),
    }
    assert any("soon" in f.detail for f in found if f.check == "token_expiring")
    later = evaluate(snap, None, None, None, now + timedelta(days=31))[0]
    assert ("token_expired", "breach") in checks(later)


def test_run_reports_tokens_without_leaking_them(cluster, tmp_path):
    private_pem, _ = key_pair(tmp_path / "keys")
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    secret = write_token(tokens, "decisions-publisher", timedelta(days=3), private_pem)
    report = run(cluster, None, tmp_path / "state", tokens)
    assert any(
        f["check"] == "token_expiring" and "decisions-publisher" in f["detail"]
        for f in report["findings"]
    )
    written = json.dumps(report) + "".join(
        p.read_text() for p in (tmp_path / "state").iterdir()
    )
    for part in secret.split("."):
        assert part not in written
