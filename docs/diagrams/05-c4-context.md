# 05 — C4 Context

```mermaid
C4Context
  title System Context: empire-platform
  Person(owner, "Owner", "Human identity of workspace owner")
  System(ep, "empire-platform", "PDP, Tasks API, outbox, Empire Graph, platform crons")
  System_Ext(web, "life-dashboard-web and owner-web", "Vercel; tasks pages, delegated tokens")
  System_Ext(hub, "platform-mcp-remote-worker", "Cloudflare tool hub, owner-mcp token")
  System_Ext(cells, "Domain cells", "finance-*, life-*, platform-*-api with own Neon DBs")
  System_Ext(idn, "platform-identity", "Identity service, shadow PDP and JWKS consumer")
  System_Ext(gw, "platform-api-gateway", "Edge proxy, checks RS256 then passes token")
  System_Ext(neon, "Neon PostgreSQL", "Project empire-platform, aws-ap-southeast-1")
  System_Ext(gh, "GitHub API", "Issues and code search for backlog-sync")
  System_Ext(sentry, "Sentry", "Errors in, issues read by backlog-sync")
  System_Ext(stripe, "Stripe API", "Balance transactions and payouts")
  System_Ext(prov, "Railway, Vercel, Cloudflare, Infisical APIs", "Token rotation targets")
  System_Ext(mon, "platform-uptime-worker", "Health probe and heartbeats, Telegram alerts")
  System_Ext(notif, "platform-notifications-api", "Queues empire-monitor alerts")

  Rel(owner, web, "Uses")
  Rel(owner, hub, "Uses through Claude")
  Rel(web, ep, "Tasks API, HTTPS JWT")
  Rel(hub, ep, "Tasks API, channel mcp")
  Rel(cells, ep, "authz decide, executions, graph events")
  Rel(gw, ep, "Proxies /v1/tasks and fetches JWKS")
  Rel(ep, idn, "Mirrors authz decide, shadow")
  Rel(ep, neon, "SQL per DB role, TLS")
  Rel(ep, gh, "Reads issues, read-only PAT")
  Rel(ep, sentry, "Sends errors, reads issues")
  Rel(ep, stripe, "Reads fees and payouts")
  Rel(ep, cells, "stripe-reconcile writes ledger and finance cells")
  Rel(ep, prov, "Pushes rotated tokens")
  Rel(mon, ep, "GET /health")
  Rel(ep, mon, "Heartbeats")
  Rel(ep, notif, "POST /v1/notifications from empire-monitor")
```

Sources: `PRODUCT.md` (Users, Operating Context), `services/platform/platform_api/routes.py`, `services/platform/platform_api/identity_shadow.py`, `src/empire_platform/backlog_sync.py` (`GITHUB_API`, `SENTRY_API`), `src/empire_platform/stripe_reconcile.py`, `src/empire_platform/hosted_rotate.py` (provider APIs), `src/empire_platform/monitor.py` (`notify`), `src/empire_platform/_sentry.py`, `contracts/automations/v1/deployments.json`, `docs/runbooks/tasks-cutover-and-rollback.md` (Neon region), ops record: api-gateway-edge (2026-11-03)

ภาพรวม C4 ระดับ workspace ทั้งหมดอยู่ที่ architecture-docs [`docs/architecture/C4-DIAGRAMS.md`](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/C4-DIAGRAMS.md) (reused, ระดับกว้างกว่า)

## Open questions

- ผู้บริโภคปัจจุบันของ Tasks API ครบหรือยัง (เช่น Go `task-sync`, TypeScript writers ที่ runbook ระบุว่า "still pending")? รายชื่อ automation ใน `automations.json` มี 46 ตัว แต่ไม่ได้บอกว่าตัวไหนยัง active
