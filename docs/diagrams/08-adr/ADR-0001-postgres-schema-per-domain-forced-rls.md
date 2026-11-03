# ADR-0001 — One PostgreSQL database, a schema per domain, forced RLS and least-privilege roles

- Status: **accepted (retro-documented)** 2026-11-03, from code on `main` `686089a`
- Context date: 2026-10-09 (first commit `0749281`)

## Context

ทุก cell ต้องเชื่อฐานกลางเดียวกันสำหรับ identity, workspace และสิทธิ์ โดย service หนึ่งต้องอ่าน schema ของ service อื่นไม่ได้ และ workspace หนึ่งต้องไม่เห็นอีก workspace แม้ application code พลาด

## Decision

- PostgreSQL 16 หนึ่ง database (`empire`) บน Neon project `empire-platform` (aws-ap-southeast-1) แยก schema `platform`, `tasks`, `graph`
- ทุกตารางที่มี `workspace_id` เปิด RLS แบบ `FORCE` โดย policy `workspace_id = platform.current_workspace()` ซึ่งอ่าน `app.workspace_id` ที่ตั้งด้วย `SET LOCAL` จาก token ที่ verify แล้ว
- DB role แยกต่อ process: `empire_migrator` (เจ้าของ schema, รัน migration), `platform_api`, `tasks_api`, `tasks_publisher`, `graph_api`, `platform_monitor`, `identity_api` ทั้งหมด `NOSUPERUSER NOBYPASSRLS NOINHERIT`
- `audit_events` append-only (trigger `platform.reject_audit_change`)
- migration ผ่าน runner ที่ตรวจ checksum (`devdb.migrate`) และ apply ครั้งเดียว

## Evidence

- `db/bootstrap/roles.sql`, `services/platform/migrations/0001_platform_core.sql`, `0002_authorization_and_rls.sql`, `services/tasks/migrations/1001_tasks_core.sql`, `services/graph/migrations/2001_graph_core.sql`
- `src/empire_platform/neon.py` (provision roles ผ่าน SQL as migrator, ไม่เป็นสมาชิก `neon_superuser`)
- `tests/test_tenant_isolation.py`, `tests/test_role_cannot_bypass_rls.py`, `tests/test_migrations.py`
- spec กลาง: architecture-docs `docs/superpowers/specs/2026-10-09-platform-foundation-identity-workflow-design.md` §0 ข้อ 1, §4

## Consequences

- RLS เป็นแนวป้องกันที่สอง; application ยังต้อง authorize ทุก request (`README.md` Security model)
- ทุก cell ใช้ compute ของ Neon project เดียวกันสำหรับ PDP: quota ของ project นี้หมดเมื่อ 2026-10-28 ทำให้ทุก cell ตอบ 503 `authorization_unavailable` (ops record: neon-platform-quota (2026-11-03))
- cell ที่มีข้อมูลของตัวเองใช้ Neon project แยก และคุยกับ platform ผ่าน HTTP (ADR-0002)
