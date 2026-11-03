# 21 — Incident response

```mermaid
flowchart TD
  subgraph DETECT["Detect"]
    A1["Uptime worker: /health down or heartbeat stale"]
    A2["Sentry error in personal-yu0"]
    A3["empire-monitor warn or breach"]
    A4["Cells answer 503 authorization_unavailable"]
  end
  A1 --> TG["Telegram book_noti_bot"]
  A2 --> BLS["backlog-sync turns error and fatal issues into tasks hourly"]
  A3 --> NOT["notifications cell alert"]
  A4 --> TRI
  TG --> TRI["Triage by owner or AI session"]
  BLS --> TRI
  NOT --> TRI
  TRI --> Q1{"Which signal"}
  Q1 -->|"Neon quota or DB down"| N1["Check Neon compute quota and endpoint; owner decides plan or waits for reset"]
  Q1 -->|"Bad deploy"| N2["Redeploy previous Railway deployment"]
  Q1 -->|"Stuck cell execution"| N3["Check cell DB, then empire-cells reconcile"]
  Q1 -->|"Token expiring or expired"| N4["Read token-rotate log, run empire-token-rotate manually"]
  Q1 -->|"Token leak"| N5["Revoke delegation now; rotate key pair is an owner decision"]
  Q1 -->|"Data damage"| N6["recovery.md isolated restore, owner decision gate"]
  Q1 -->|"MCP issuer abuse"| N7["Unset TASKS_API_MCP_PUBLIC_KEY_PEM and redeploy tasks-api"]
  N1 --> FIX["Fix on a branch, CI green, merge locally, approve Railway deployment"]
  N2 --> FIX
  N3 --> PM
  N4 --> PM
  N5 --> PM
  N6 --> PM
  N7 --> PM
  FIX --> PM["Incident review Markdown with Incident ID and actions"]
  PM --> BLS2["backlog-sync files source turns decided actions into tasks"]
```

Sources: `docs/runbooks/cell-executions-and-tokens.md`, `docs/runbooks/recovery.md`, `docs/runbooks/remote-mcp.md` (Kill switch), `README.md` (roll back by redeploying an earlier deployment), `src/empire_platform/backlog_sync.py` (Sentry + incident review sources), `src/empire_platform/monitor.py` (`notify`), ops record: neon-platform-quota (2026-11-03), ops record: prod-baseline-monitoring-backups (2026-11-03), ops record: railway-deploy-approval-gate (2026-11-03)

## Past incidents ที่บันทึกใน repo

| Date | Incident | Response recorded |
|---|---|---|
| 2026-10-09 | `pg_dump` traceback printed the migrator URL | migrator password rotated via Neon API; errors redacted (`tasks-cutover-and-rollback.md`) |
| 2026-10-09 | tests wrote to production for ~10 min (63 test tasks) | found via outbox and execution log, reverted through the API; tests isolated with `EMPIRE_CLIENT_CONFIG` |
| 2026-10-28 | Neon compute quota exhausted, every cell 503 | publisher moved to hourly cron (PR #16); owner waited for 11-01 reset (ops record neon-platform-quota) |

## Open questions

- ไม่มี severity ladder, on-call หรือ incident template ใน repo นี้; ใช้ template กลาง (`Incident ID/severity` ที่ backlog-sync อ่าน) หรือไม่
- ใครเป็นผู้ approve deployment ระหว่าง incident (owner หรือ AI) ภายใต้ `NEEDS_APPROVAL`
