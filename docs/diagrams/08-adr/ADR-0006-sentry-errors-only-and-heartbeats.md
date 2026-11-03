# ADR-0006 — Sentry errors only with scrubbing; cron jobs report heartbeats

- Status: **accepted (retro-documented)** 2026-11-03
- Related central ADRs (reused): architecture-docs [ADR-0002 production baseline](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0002-production-baseline-monitoring-backups.md), [ADR-0006 backlog sync host](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0006-backlog-sync-host.md)

## Decision

- `empire_platform/__init__.py` เรียก `init_sentry()` จึงครอบทุก entry point; no-op ถ้าไม่มี `SENTRY_DSN`
- ไม่ส่ง locals, body, header, cookie, query, URL; tracing ปิด; scrub URL password, `key=value` secrets, Bearer และ JWT ก่อนส่ง
- handler ที่กลืน error เรียก `capture()` (publisher หนึ่ง event ต่อ sweep / failure streak)
- `backlog-sync`, `stripe-reconcile`, `token-rotate` POST `MONITOR_URL/heartbeat/<job>` ด้วย `HEARTBEAT_TOKEN`; dry run ของ `token-rotate` ไม่ส่ง heartbeat (PR #10); `recurring-gen` ไม่มี heartbeat ในโค้ด

## Evidence

`src/empire_platform/_sentry.py`, `src/empire_platform/__init__.py`, `services/tasks/tasks_api/publisher.py`, `src/empire_platform/{backlog_sync,stripe_reconcile,hosted_rotate}.py` (`heartbeat`), `tests/test_sentry.py`, PR #4–#8 (`feat/sentry`, `fix/sentry-*`), PR #10 `fix/rotate-dry-run-heartbeat`
