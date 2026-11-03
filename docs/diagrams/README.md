# Core 22 SDLC diagram pack — platform-empire

ชุดภาพ Core 22 ตาม SDLC Diagram Catalog v2 (knowledge-base `knowledge/tech/core-cs/sdlc-diagram-catalog.md`) วาดจากโค้ดและ config ใน repo นี้ ณ `origin/main` `686089a` (2026-11-03)
repo นี้คือ **empire-platform**: monorepo ของ control plane กลาง (identity/workspace/delegation, PDP `platform.authorize`, Tasks API, outbox → Empire Graph) และ Railway services/crons 6 ตัวใน project `platform-empire` ที่ build จาก image เดียว

ทุกไฟล์มี `Sources:` ใต้ภาพ และ "Open questions" สำหรับสิ่งที่ยืนยันจากโค้ดไม่ได้; ops records อ้างเป็น `ops record: <file-stem> (2026-11-03)`

| # | Item | Status | File | Sources read |
|---|---|---|---|---|
| 01 | Business process As-Is / To-Be | done | [01-business-process.md](01-business-process.md) | `tasks_api/app.py`, `.railway/railway.ts`, jobs in `src/empire_platform/`, `platform_api/routes.py`, runbooks, central platform-foundation spec |
| 02 | Use cases | done | [02-use-cases.md](02-use-cases.md) | routes, `auth.py`, `roles.sql`, `0001_platform_core.sql`, `automations.json`, `task-deletion.md` |
| 03 | User flows | done (API/CLI flows; no UI in repo) | [03-user-flows.md](03-user-flows.md) | routes, `empire_mcp/server.py`, `recurring.py`, `backlog_sync.py`, `cells.py`, `README.md` |
| 04 | Traceability (RTM) | done | [04-traceability.md](04-traceability.md) | `PRODUCT.md`, spec §8.1, modules, `tests/`, `ci.yml`, `railway.ts` |
| 05 | C4 Context | done | [05-c4-context.md](05-c4-context.md) | `PRODUCT.md`, integration modules, `deployments.json`, ops record api-gateway-edge |
| 06 | C4 Container | done | [06-c4-container.md](06-c4-container.md) | `railway.ts`, `Dockerfile`, `docker-entrypoint.sh`, `pyproject.toml`, mains |
| 07 | C4 Component | done | [07-c4-component.md](07-c4-component.md) | `tasks_api/*`, `platform_api/*`, `empire_authz`, `jwks.py`, `_sentry.py`, job imports |
| 08 | ADRs | done (6) + reused central ADR-0002/0003/0006 | [08-adr/](08-adr/) | migrations, `app.py`, `publisher.py`, `railway.ts`, `auth.py`, `hosted_rotate.py`, `_sentry.py`, PR list |
| 09 | ERD | done (3 schemas) | [09-erd.md](09-erd.md) | `services/*/migrations/*.sql` |
| 10 | Sequences | done (4) | [10-sequences.md](10-sequences.md) | `app.py`, `routes.py`, `publisher.py`, `consumer.py`, `hosted_rotate.py` |
| 11 | State machines | done (7) | [11-state-machines.md](11-state-machines.md) | migrations (CHECKs), `app.py`, `cells.py`, `backlog-sync.md` |
| 12 | API & integrations | done; reused OpenAPI contract | [12-api-integration.md](12-api-integration.md) → [`contracts/tasks/v1/openapi.json`](../../contracts/tasks/v1/openapi.json) | routes, outbound API constants, `deployments.json` |
| 13 | Threat model | done | [13-threat-model.md](13-threat-model.md) | `auth.py`, `roles.sql`, RLS migrations, `hosted_rotate.py`, `_sentry.py`, security workflow |
| 14 | Backup & DR | reused + summarized | [14-backup-dr.md](14-backup-dr.md) → [`docs/runbooks/recovery.md`](../runbooks/recovery.md) | `recovery.md`, `tasks-cutover-and-rollback.md`, `test_recovery_rehearsal.py` |
| 15 | CI/CD pipeline | done | [15-cicd-pipeline.md](15-cicd-pipeline.md) | `ci.yml`, platform-ci-workflows `ci-python.yml` / `security.yml` @ `87dc5e1c`, `railway.ts` |
| 16 | Deployment | done | [16-deployment.md](16-deployment.md) | `railway.ts`, `Dockerfile`, `PRODUCT.md`, runbooks, `neon.py` |
| 17 | Test coverage matrix | done | [17-test-coverage-matrix.md](17-test-coverage-matrix.md) | `tests/*.py`, `conftest.py`, `graph_world.py`, `ci.yml` |
| 18 | Git & release workflow | done | [18-git-release-workflow.md](18-git-release-workflow.md) | `git log --first-parent`, `gh pr list` |
| 19 | Service dependency map | done | [19-service-dependency-map.md](19-service-dependency-map.md) | `railway.ts`, integration modules, ops records |
| 20 | Observability | done | [20-observability.md](20-observability.md) | `_sentry.py`, `monitor.py`, heartbeat functions, runbooks |
| 21 | Incident response | done | [21-incident-response.md](21-incident-response.md) | runbooks, `backlog_sync.py`, `monitor.py`, ops records |
| 22 | DORA | done (5/5 computed from git + CI, Railway side not pulled) | [22-dora.md](22-dora.md) | `git log`, `gh run list` (69 runs) |

## ADR list

- [ADR-0001](08-adr/ADR-0001-postgres-schema-per-domain-forced-rls.md) PostgreSQL schema per domain + forced RLS + least-privilege roles
- [ADR-0002](08-adr/ADR-0002-single-authorization-decision-point.md) `platform.authorize` เป็น decision point เดียว, write ตรวจสองครั้ง
- [ADR-0003](08-adr/ADR-0003-transactional-outbox-to-graph.md) Transactional outbox → idempotent graph consumer, publisher รายชั่วโมง
- [ADR-0004](08-adr/ADR-0004-one-image-railway-cd-wait-for-ci.md) Image เดียว, Railway CD จาก `main` หลัง CI
- [ADR-0005](08-adr/ADR-0005-platform-jwt-delegations-hosted-rotation.md) RS256 JWT + delegation, token 30 วัน, hosted weekly rotation
- [ADR-0006](08-adr/ADR-0006-sentry-errors-only-and-heartbeats.md) Sentry errors only + heartbeats

Central ADRs ที่เกี่ยวข้อง (reused, ไม่ copy): architecture-docs [ADR-0002](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0002-production-baseline-monitoring-backups.md), [ADR-0003](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0003-hosted-shared-services.md), [ADR-0006](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0006-backlog-sync-host.md)

## Validation

`node ~/work/core22/tools/validate.mjs docs/diagrams/*.md docs/diagrams/08-adr/*.md`
