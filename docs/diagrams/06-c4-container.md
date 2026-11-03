# 06 — C4 Container

ทุก container ใน Railway build จาก `Dockerfile` เดียวกัน และเลือก process ด้วย `EMPIRE_SERVICE` หรือ `startCommand` ใน `.railway/railway.ts`

```mermaid
C4Container
  title Containers: empire-platform
  Person(owner, "Owner")
  System_Ext(cells, "Domain cells and web clients")
  System_Ext(ext, "GitHub, Sentry, Stripe, provider APIs")
  System_Ext(mon, "Uptime monitor")

  Container_Boundary(rw, "Railway project platform-empire") {
    Container(api, "tasks-api", "Python 3.12, FastAPI, uvicorn", "Tasks API, platform HTTP routes, JWKS, /health")
    Container(pub, "tasks-publisher", "Python cron 5 * * * *", "empire-publish --batch 1000, outbox to graph")
    Container(rec, "recurring-gen", "Python cron 15 17 * * *", "Generates recurring occurrences via the API")
    Container(bl, "backlog-sync", "Python cron 5 * * * *", "GitHub, Sentry, reviews to tasks")
    Container(sr, "stripe-reconcile", "Python cron 30 2 * * *", "Stripe fees and payouts to finance cells")
    Container(rot, "token-rotate", "Python cron 30 19 * * 0", "Mints and pushes automation tokens")
  }
  ContainerDb(db, "empire database", "Neon PostgreSQL 16", "Schemas platform, tasks, graph; forced RLS")
  Container(mcp, "empire-mcp stdio", "Python, MCP SDK", "Local tasks.* and graph.* tools on the owner machine")
  Container(cli, "Operator CLIs", "Python console scripts", "empire-db, empire-neon, empire-cells, empire-automations, empire-monitor")

  Rel(owner, mcp, "stdio")
  Rel(mcp, api, "HTTPS, caller token")
  Rel(cells, api, "HTTPS JWT")
  Rel(api, db, "role tasks_api")
  Rel(pub, db, "roles tasks_publisher and graph_api")
  Rel(rec, api, "HTTPS, recurring-gen token")
  Rel(bl, api, "HTTPS, backlog-sync token")
  Rel(bl, ext, "Reads")
  Rel(sr, ext, "Reads Stripe, writes finance cells")
  Rel(rot, db, "roles empire_migrator and platform_api")
  Rel(rot, ext, "Pushes tokens")
  Rel(cli, db, "Local cluster or hosted URLs from db.env")
  Rel(mon, api, "GET /health")
```

Sources: `.railway/railway.ts`, `Dockerfile`, `docker-entrypoint.sh`, `pyproject.toml` (`[project.scripts]`, dependencies), `services/tasks/tasks_api/app.py` (`main`), `services/tasks/tasks_api/publisher.py` (`main`), `services/mcp/empire_mcp/server.py`, `src/empire_platform/hosted_rotate.py` (environment: `EMPIRE_DB_URL_EMPIRE_MIGRATOR`, `EMPIRE_DB_URL_PLATFORM_API`), `db/bootstrap/roles.sql`, `docs/runbooks/tasks-cutover-and-rollback.md`

## หมายเหตุ

- Graph read API (`services/graph/graph_service/app.py`: `/v1/graph/neighbors`, `/v1/graph/search`, `/v1/graph/consents`) มี `create_app` แต่ **ไม่มี console script หรือ Railway service** ที่ serve มัน; ใน production มีแค่ consumer (ใน publisher) และ `POST /v1/graph/events` (ใน tasks-api)
- `empire-mcp-http` (hosted MCP, Clerk OAuth) ปลดระวาง 2026-10-18 ไม่เคย deploy (`docs/runbooks/remote-mcp.md`); โค้ดยังอยู่
- โฟลเดอร์ `build/lib/` เป็นสำเนา build เก่าที่ commit ไว้ (ไม่มี `backlog_sync.py`, ไฟล์อื่นต่างจาก `src/`); Dockerfile ไม่ COPY มัน

## Open questions

- ควรลบ `build/lib/` ออกจาก git หรือไม่ (mypy exclude ไว้แล้ว `exclude = ["^build/"]`)
- Graph read API จะ deploy เมื่อไร หรือ local `empire-mcp` ชี้ `EMPIRE_GRAPH_API` (default `http://127.0.0.1:3602`) ไปที่ใด
