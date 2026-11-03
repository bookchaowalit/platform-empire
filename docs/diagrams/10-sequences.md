# 10 — Sequence diagrams

## 1. Owner หรือ automation แก้ task (`PATCH /v1/tasks/{id}`)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant A as tasks-api
  participant DB as Neon, role tasks_api
  C->>A: PATCH /v1/tasks/id, Bearer JWT, If-Match version
  A->>A: TokenVerifier: RS256, aud tasks-api, exp, lifetime cap
  A->>A: scope check against token scope claim
  A->>DB: tx1 SET LOCAL app.workspace_id, SELECT platform.authorize lock false
  DB-->>A: allow, deny or require_approval, decision recorded
  alt deny or require_approval
    A-->>C: 403 or 409, or 404 when the task is not visible
  else allow
    A->>DB: tx2 SELECT tasks.writes_enabled()
    A->>DB: SELECT platform.begin_execution
    A->>DB: enforce: platform.authorize lock true
    A->>DB: SELECT task FOR UPDATE, compare version
    A->>DB: UPDATE tasks.tasks, version plus 1
    A->>DB: INSERT tasks.outbox tasks.task.updated
    A->>DB: SELECT platform.record_audit before and after hash
    A->>DB: SELECT platform.finish_execution succeeded, COMMIT
    A-->>C: 200 Task
  end
```

Sources: `services/tasks/tasks_api/app.py` (`principal`, `precheck`, `acting`, `emit`, PATCH route line 1003), `services/tasks/tasks_api/auth.py`, `packages/authz/empire_authz/__init__.py`, `services/platform/migrations/0003_authorization_decision_point.sql`

## 2. Cell ที่มี DB ของตัวเองเขียนข้อมูล (เช่น solo-decisions)

```mermaid
sequenceDiagram
  autonumber
  participant U as Caller
  participant CE as Cell API
  participant CDB as Cell Neon DB
  participant P as tasks-api platform routes
  participant PDB as Platform DB
  participant ID as platform-identity
  U->>CE: write request with Bearer token
  CE->>P: POST /v1/executions, same token, aud platform-api, Idempotency key
  P->>PDB: begin_cell_execution and enforce with locks
  PDB-->>P: execution_id or replayed
  P-->>CE: 201 execution
  CE->>CDB: commit rows and cell outbox tagged with execution_id
  CE->>P: POST /v1/executions/id/finish, status, action, row hashes
  P->>PDB: finish_cell_execution writes audit_events
  P-->>CE: 200
  Note over CE,P: If finish is lost, execution stays running and the monitor warns
  CE->>P: cell publisher POST /v1/graph/events, aud graph-api
  P->>PDB: graph.ingest_cell_event, own namespace only
  opt read or precheck
    CE->>P: POST /v1/authz/decide
    P-->>CE: decision
    P--)ID: background mirror when IDENTITY_SHADOW_URL is set
  end
```

Sources: `services/platform/platform_api/routes.py`, `services/platform/platform_api/identity_shadow.py`, `services/platform/migrations/0006_cell_executions.sql`, `services/graph/migrations/2004_cell_ingest.sql`, `docs/runbooks/cell-executions-and-tokens.md`

## 3. Outbox → Empire Graph (cron ทุกชั่วโมง)

```mermaid
sequenceDiagram
  autonumber
  participant R as Railway cron 5 * * * *
  participant PB as empire-publish --batch 1000
  participant DB as Neon
  R->>PB: start container
  PB->>DB: as tasks_publisher: SELECT outbox unpublished, due, oldest first, FOR UPDATE SKIP LOCKED
  loop each event
    PB->>DB: as graph_api: set app.workspace_id, SELECT graph.ingest_event
    alt node, edge, deleted or stale
      PB->>DB: SELECT graph.record_correlation, COMMIT
      PB->>DB: UPDATE outbox SET published_at
    else duplicate
      PB->>DB: UPDATE outbox SET published_at
    else consumer error
      PB->>DB: attempts plus 1, next_attempt_at backoff max 600 s, last_error
      PB->>PB: capture to Sentry once per sweep
    end
  end
  PB-->>R: exit 0, or 1 when any event failed
```

Sources: `services/tasks/tasks_api/publisher.py`, `services/graph/graph_service/consumer.py`, `services/graph/migrations/2005_ingest_version_guard.sql`, `.railway/railway.ts`

## 4. Weekly token rotation (`token-rotate`)

```mermaid
sequenceDiagram
  autonumber
  participant R as Railway cron 30 19 * * 0
  participant T as empire-token-rotate-hosted
  participant DB as Platform DB
  participant P as Railway, Vercel, Cloudflare, Infisical APIs
  participant M as Uptime monitor
  R->>T: start
  T->>P: preflight provider tokens
  T->>P: read exp of Railway copies only, per deployments.json target
  T->>T: due = exp within 10 days, unreadable, no Railway copy, or EMPIRE_ROTATE_FORCE
  T->>DB: provision_mint: renew delegation, mint 30-day RS256 tokens
  loop each target of a due automation
    T->>P: push token in request body
  end
  T->>M: POST /heartbeat/token-rotate ok or failed
```

Sources: `src/empire_platform/hosted_rotate.py` (docstring, `push`, `provision_mint`, `heartbeat`), `src/empire_platform/rotate.py`, `contracts/automations/v1/deployments.json`, `tests/test_hosted_rotate.py`

หมายเหตุข้อ 4: อ่าน exp ได้เฉพาะสำเนาบน Railway (`due_hosted`); automation ที่ไม่มีสำเนาบน Railway (เช่น `owner-mcp` บน Cloudflare + Infisical, `solo-web` บน Vercel) ถูก rotate ทุกรอบ

## Open questions

- ไม่มี
