# 19 — Service dependency map (runtime)

```mermaid
flowchart LR
  subgraph EP["platform-empire Railway services"]
    API["tasks-api"]
    PUB["tasks-publisher"]
    REC["recurring-gen"]
    BL["backlog-sync"]
    SR["stripe-reconcile"]
    ROT["token-rotate"]
  end
  NEON[("Neon empire-platform")]
  IDN["platform-identity"]
  GH["GitHub REST"]
  SEN["Sentry API and ingest"]
  STR["Stripe API"]
  CRM["crm-api ledger"]
  FIN["finance accounts, transactions, reconciliations cells"]
  RWAPI["Railway GraphQL"]
  VC["Vercel API"]
  CF["Cloudflare API"]
  INF["Infisical"]
  UP["platform-uptime-worker heartbeats"]

  API --> NEON
  API -.->|"optional shadow"| IDN
  PUB --> NEON
  REC --> API
  BL --> API
  BL --> GH
  BL --> SEN
  BL --> UP
  SR --> STR
  SR --> CRM
  SR --> FIN
  SR --> UP
  ROT --> NEON
  ROT --> RWAPI
  ROT --> VC
  ROT --> CF
  ROT --> INF
  ROT --> UP
  API --> SEN
  PUB --> SEN

  subgraph DEP["Depend on empire-platform"]
    CELLS["Domain cells: PDP, executions, graph events, JWKS"]
    WEB["owner-web and accounts-web"]
    HUB["Tool hub Worker"]
    GW["platform-api-gateway"]
  end
  CELLS --> API
  WEB --> API
  HUB --> API
  GW --> API
```

Sources: `.railway/railway.ts`, `services/platform/platform_api/*.py`, `src/empire_platform/{backlog_sync,stripe_reconcile,hosted_rotate,recurring,_sentry}.py`, `services/tasks/tasks_api/publisher.py`, `contracts/automations/v1/deployments.json`, ops record: api-gateway-edge (2026-11-03), ops record: neon-platform-quota (2026-11-03)

## Critical path

`Neon empire-platform` → `tasks-api /v1/authz/decide` → ทุก cell ที่ใช้ PDP นี้ เมื่อ Neon compute quota หมด (2026-10-28 → 11-01) ทุก cell ตอบ 503 `authorization_unavailable` (ops record neon-platform-quota)

## Open questions

- cell ใดบ้างยังเรียก PDP นี้ และ cell ใดย้ายไป `platform-identity` แล้ว (ops record book-platforms-status: 8 platform services เชื่อ identity issuer ตั้งแต่ 2026-10-29)
