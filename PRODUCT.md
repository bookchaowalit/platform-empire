# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The Solo Empire owner, through `life-dashboard-web` (tasks pages), the local `empire-mcp` stdio
  server and the hosted tool hub (`platform-mcp-remote-worker`).
- Every domain cell (`finance-*`, `life-*`, `platform-*-api`): they authorize each request,
  record executions and audit events, and publish graph events through this platform.
- Automations with an `empire-platform` token (for example `owner-mcp`, `recurring-gen`,
  `evening-checkin`), acting only through delegations whose scopes are a subset of the
  grantor's live permissions.

## Product Purpose

The shared foundation every cell trusts: identities, workspaces, roles, delegations and
approvals; one authorization decision point (`platform.authorize`); the Tasks API, which is the
owner's single task backlog; and the Empire Graph that links records across cells.

Success: every write in every cell is authorized here and leaves an audit event, tasks have one
source of truth, and no token outlives its rotation window.

## Positioning

A small, owner-run control plane instead of a hosted identity or task SaaS: PostgreSQL with
forced row-level security per workspace as the second line of defence, deny-by-default
authorization that every language calls the same way (`contracts/authz/v1/`), and a
transactional outbox so tasks and graph events never drift.

## Operating Context

- Production: `https://tasks-api-production-4785.up.railway.app` (`/health`), Railway project
  `platform-empire` (services `tasks-api`, `tasks-publisher`, crons `recurring-gen` and
  `token-rotate`), Neon PostgreSQL.
- Deploys: merging to `main` deploys every service after CI (Railway GitHub source with Wait for
  CI). Never `railway up` from a working tree.
- Monitored by `platform-uptime-worker` and `empire-monitor`; errors go to Sentry without
  personal data. Automation tokens live 30 days and rotate weekly (`token-rotate`).
- Development and tests use a disposable local PostgreSQL (`pgserver`) with synthetic data.

## Capabilities and Constraints

- Authorization: `platform.authorize` denies by default, answers `resource_not_found` for other
  workspaces, follows delegation revocation and approval policies, and records every decision.
  Writes check twice (a recorded precheck, then `enforce` with row locks in the write
  transaction).
- Tasks API (`tasks.v1`, contract in `contracts/tasks/v1/`): tasks, relations, recurring
  occurrences, archive, and idempotent upsert by external source and id.
- Empire Graph: nodes, aliases, edges and consents fed by the outbox publisher (at least once,
  oldest first, retry with backoff).
- MCP: `tasks.*` and `graph.*` tools that call the APIs with the caller's token and reject
  unknown arguments.
- Out of scope: end-user sign-up and passwords (identity is `platform-identity`), and domain
  data (each cell owns its own database).

## Measures

- Writes in cells without a matching authorization decision and audit event (target: 0).
- Outbox lag: age of the oldest unpublished event (`empire-monitor`; [ต้องยืนยัน] threshold).
- Automation tokens expiring within 10 days that the weekly rotation did not renew (target: 0).
- Open tasks with no update in 30 days, as a backlog health signal [ต้องยืนยัน].
- 5xx rate and p95 latency on `tasks-api` (Sentry and uptime worker).
