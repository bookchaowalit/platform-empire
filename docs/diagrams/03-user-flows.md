# 03 — User flows per role

repo นี้เป็น API และ jobs ไม่มีหน้าเว็บ; "flow" จึงเป็นลำดับการเรียก route และ CLI ต่อ role (หน้าจอ tasks อยู่ใน `life-dashboard-web` นอก repo)

## 1. Owner: วงจรงานหนึ่งชิ้น (ผ่าน API, local `empire-mcp` หรือ tool hub)

```mermaid
flowchart TD
  A["GET /v1/tasks, filters: status, project, archived"] --> B["POST /v1/tasks, 201, number assigned"]
  B --> C["PATCH /v1/tasks/id with If-Match version"]
  C --> D["POST /v1/tasks/id/links, URN target"]
  C --> E["PUT /v1/tasks/id/assignments or dependencies"]
  C --> F["POST /v1/tasks/id/complete"]
  F --> G{"Keep or clean up"}
  G -->|"soft delete"| H["POST /v1/tasks/id/archive with reason"]
  H --> I["POST /v1/tasks/id/unarchive"]
  G -->|"hard delete, human only"| J["DELETE /v1/tasks/id, tasks.task:delete plus approved approval"]
  J -->|"no approval"| K["409 require_approval"]
```

Sources: `services/tasks/tasks_api/app.py` (routes at lines 790–1844), `services/mcp/empire_mcp/server.py` (tools `tasks.list/get/create/update/complete/link`, `graph.search/neighbors`), `docs/policies/task-deletion.md`, `contracts/tasks/v1/openapi.json`

MCP tools ไม่มี delete และ archive (`server.py`; `docs/policies/task-deletion.md` "the new MCP has no delete tool")

## 2. Automation: recurring-gen (daily) และ backlog-sync (hourly)

```mermaid
flowchart TD
  subgraph REC["recurring-gen, 00:15 Bangkok"]
    R1["GET /v1/tasks, find recurring parents"] --> R2["POST /v1/tasks/id/occurrences/generate per parent"]
    R2 -->|"unique parent, occurrence"| R3["Re-run creates nothing twice"]
    R2 -->|"one parent fails"| R4["Log, skip, exit 1 at end"]
  end
  subgraph BL["backlog-sync, every hour at :05"]
    B1["Read GitHub issues, Sentry issues, review Markdown"] --> B2["GET /v1/tasks/external/source/key"]
    B2 -->|"missing"| B3["PUT /v1/tasks/external/source/key, create with priority and due"]
    B2 -->|"sync-owned field differs"| B4["PUT with If-Match, update only those fields"]
    B2 -->|"same"| B5["unchanged, no outbox event"]
    B3 --> B6["Heartbeat backlog-sync"]
    B4 --> B6
    B5 --> B6
  end
```

Sources: `src/empire_platform/recurring.py`, `src/empire_platform/backlog_sync.py`, `docs/runbooks/backlog-sync.md` (reused), `.railway/railway.ts`

## 3. Domain cell ที่มี DB ของตัวเอง

```mermaid
flowchart LR
  C1["Read: POST /v1/authz/decide"] --> C2["Write: POST /v1/executions, Idempotency per key"]
  C2 --> C3["Commit in the cell's own DB"]
  C3 --> C4["POST /v1/executions/id/finish with row hashes"]
  C4 --> C5["Cell publisher: POST /v1/graph/events"]
  C3 -.->|"finish lost"| C6["Execution stays running, monitor warns after 15 min"]
  C6 --> C7["Owner: empire-cells reconcile --succeed or --fail"]
```

Sources: `services/platform/platform_api/routes.py`, `src/empire_platform/cells.py`, `docs/runbooks/cell-executions-and-tokens.md` (reused)

## 4. Operator (owner workstation)

```mermaid
flowchart TD
  O1["empire-db start: local pgserver cluster, roles, migrations"] --> O2["pytest, empire-contracts --check"]
  O3["empire-neon provision and verify"] --> O4["empire-automations keys and provision"]
  O4 --> O5["empire-cells grant --cell NAME"]
  O6["empire-monitor --api URL --notify"] --> O7{"level"}
  O7 -->|"warn or breach"| O8["Queue alert in notifications cell"]
  O9["empire-token-rotate, manual fallback"] --> O10["Push tokens to deployments.json targets"]
```

Sources: `README.md` (Local development, Token rotation), `src/empire_platform/{devdb,neon,automations,cells,monitor,rotate}.py`, `pyproject.toml` `[project.scripts]`

## Open questions

- `empire-monitor` รันที่ไหนวันนี้? runbook บอกว่า crontab ของ owner รันทุกวัน 02:17 (historical, 2026-10-09) แต่ไม่มี Railway service หรือ workflow ของ monitor ใน repo นี้
- ไม่มี flow ของ owner บน UI ใน repo; ต้องอ้างอิง `life-dashboard-web` ถ้าต้องการ screen flow
