# ADR-0003 — Transactional outbox, at-least-once publisher, idempotent graph consumer

- Status: **accepted (retro-documented)** 2026-11-03

## Context

task กับ graph ต้องไม่ drift (`PRODUCT.md` Positioning) และไม่มี message broker ใน stack

## Decision

- ทุก write เขียนแถว `tasks.outbox` ใน transaction เดียวกับข้อมูล
- `empire-publish` (role `tasks_publisher`) อ่านแถวที่ `published_at IS NULL AND next_attempt_at <= now()` เรียงเก่าสุดก่อน `FOR UPDATE SKIP LOCKED`, ส่งให้ `GraphConsumer` (role `graph_api`, `graph.ingest_event`), fail → `attempts` +1 และ backoff สูงสุด 600 s
- consumer idempotent ด้วย `graph.inbox (consumer, event_id)` → `duplicate`; version guard ป้องกัน event เก่าทับใหม่ (`stale`)
- ตั้งแต่ 2026-10-28 publisher เป็น Railway cron `5 * * * *` (`--batch 1000`) แทน loop ทุก 5 s เพื่อให้ Neon compute ได้ suspend; graph lag ได้ถึง 1 ชั่วโมง

## Evidence

- `services/tasks/migrations/1001_tasks_core.sql` (outbox), `1002_writer_parity.sql` (attempts, next_attempt_at, last_error), `1003_outbox_publisher.sql`
- `services/tasks/tasks_api/publisher.py`, `services/graph/graph_service/consumer.py`, `services/graph/migrations/2001_graph_core.sql`, `2005_ingest_version_guard.sql`
- `.railway/railway.ts` (comment "Hourly cron run (2026-10-28)"), PR #16 `ops/publisher-hourly-cron`
- `tests/test_graph.py`, `tests/test_recovery_rehearsal.py`

## Consequences

- monitor threshold "outbox oldest unpublished > 15 min warn / > 60 min breach" (`monitor.py`) ตอนนี้ชนกับ cron รายชั่วโมงโดยออกแบบ ดู Open questions ใน [20-observability.md](../20-observability.md)
