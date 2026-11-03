# ADR-0002 — `platform.authorize` is the only decision point; writes check twice

- Status: **accepted (retro-documented)** 2026-11-03

## Context

API, MCP, automations และ cell ในภาษาอื่นต้องได้คำตัดสินเดียวกัน (spec 8.1 #4) และ revocation ระหว่าง check กับ write ต้องถูกจับ

## Decision

- ฟังก์ชัน SQL `platform.authorize(actor, workspace, action, resource, delegation, on_behalf_of, approval, channel, lock)` เป็นที่เดียวที่ตัดสิน: deny by default, `resource_not_found` สำหรับ workspace อื่น, ตาม delegation (subset ของ grantor, live), policy `requires_approval`, และบันทึกทุกคำตัดสินใน `platform.authorization_decisions`
- Python client `empire_authz.authorize` / `enforce`; ภาษาอื่นใช้ contract `contracts/authz/v1/` (+ conformance cases)
- write = precheck แบบ unlocked ที่บันทึกแล้ว → transaction เดียว: `begin_execution` → `enforce` (lock) → DML → outbox → `record_audit` → `finish_execution`
- cell ที่มี DB ของตัวเองได้กติกาเดียวกันผ่าน HTTP (`/v1/authz/decide`, `/v1/executions`, `/finish`) โดยส่ง token ของ caller ต่อ

## Evidence

- `services/platform/migrations/0003_authorization_decision_point.sql`, `0006_cell_executions.sql`, `0007_reconcile_cell_executions.sql`
- `packages/authz/empire_authz/__init__.py`, `services/tasks/tasks_api/app.py` (`precheck`, `acting`), `services/platform/platform_api/routes.py`
- `tests/test_authz.py`, `tests/test_delegation.py`, `tests/test_platform_http.py`, `contracts/authz/v1/conformance.json`

## Consequences

- cross-database write ของ cell ไม่ atomic: finish ที่หายทำให้ execution ค้าง `running` → monitor + `empire-cells reconcile`
- tasks-api เป็น single point of failure ของทุก cell ที่เรียก `/v1/authz/decide`
- กำลังย้ายไป `platform-identity`: shadow mirror (`IDENTITY_SHADOW_URL`) เปรียบเทียบคำตัดสินโดย caller ไม่ต้องรอ
