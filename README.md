# empire-platform

Identity, workspace, authorization, workflow, and graph foundation for the
Solo Empire domain cells. Architecture and decisions live in the sibling
`architecture-docs` checkout (or `SOLO_ARCHITECTURE_ROOT`):

- ADR: `docs/superpowers/specs/2026-10-09-platform-foundation-identity-workflow-design.md`
- Domain Cells + Empire Graph: `docs/superpowers/specs/2026-10-09-domain-cells-empire-graph-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-10-09-platform-foundation-phase-1.md`

Status: production cutover to Neon/Railway and retirement of the SQLite task
backend were recorded on 2026-10-09. The platform includes shared authorization,
the Tasks API, transactional outbox/publisher, Empire Graph and MCP adapter.
Development and tests use disposable local PostgreSQL with synthetic data.
See [recovery](docs/runbooks/recovery.md) for the executable local restore drill
and the separate hosted recovery procedure; hosted restore readiness still
requires its own evidence.

## Local development

PostgreSQL comes from the `pgserver` wheel (16.x) unless `EMPIRE_PG_BIN` points
at other binaries. The cluster lives in `.runtime/pgdata` and listens only on a
Unix socket.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/empire-db start      # init, start, bootstrap roles, apply migrations
.venv/bin/empire-db psql       # connect as platform_api
.venv/bin/empire-db stop
.venv/bin/python -m pytest     # each run uses its own throwaway cluster
.venv/bin/empire-contracts --check
.venv/bin/tasks-api            # http://127.0.0.1:3601, tokens from empire-dev-token
```

## Recurring tasks

`empire-recurring [--dry-run] [--until YYYY-MM-DD]` (`src/empire_platform/recurring.py`)
generates the due instances of recurring tasks. It runs as the `recurring-gen`
Railway cron service (`15 17 * * *` UTC, i.e. 00:15 Asia/Bangkok, the schedule of
the retired local "Generate Recurring" job) and calls the Tasks API over HTTPS
with the `recurring-gen` automation token: it lists tasks, then
`POST /v1/tasks/{id}/occurrences/generate` per recurring parent, which is
idempotent (unique parent and occurrence), so re-runs create nothing twice.
Zero recurring tasks is a successful no-op; one failing parent is logged and
skipped (exit 1 at the end). Variables (Railway only): `EMPIRE_SERVICE=recurring`,
`EMPIRE_TASKS_API_URL`, `RECURRING_GEN_TOKEN`. Deploy:
`railway up --ci --service recurring-gen`; check:
`railway run --service recurring-gen -- empire-recurring --dry-run`.

Railway variables for `tasks-api`, `tasks-publisher` and `recurring-gen` also include
`SENTRY_DSN` (error reporting; unset = off, see `src/empire_platform/_sentry.py`) and
`SENTRY_ENVIRONMENT`.

## Layout

| Path | Contents |
|---|---|
| `db/bootstrap/roles.sql` | cluster roles: `empire_migrator` (owns schemas, runs migrations) and `platform_api` (runtime, no BYPASSRLS, owns nothing) |
| `services/platform/migrations/` | `platform` schema: identities, workspaces, memberships, roles, policies, delegations, approvals, executions, audit events, RLS, and grants |
| `src/empire_platform/devdb.py` | local cluster control and the checksum-verified migration runner |
| `services/platform/migrations/0003_*` | `platform.authorize` policy decision point, decision log, and SECURITY DEFINER entry points for domain services |
| `contracts/authz/v1/` | language-neutral authorization request/decision schemas and conformance cases |
| `packages/authz/` | Python client (`authorize`, `enforce`) for the decision point |
| `services/tasks/migrations/` | `tasks` schema with forced RLS, URN links, and the transactional outbox |
| `services/tasks/tasks_api/` | Tasks API (`tasks.v1`); contract in `contracts/tasks/v1/` |
| `services/tasks/tasks_api/publisher.py` | outbox publisher (`tasks_publisher` role): oldest first, retry with backoff, at least once |
| `services/graph/` | graph schema (nodes, aliases, edges, relation types, consents, inbox), event consumer, and the read API |
| `services/mcp/empire_mcp/` | MCP tools `tasks.*` and `graph.*` that call the APIs with the caller's token and reject unknown arguments; `hosted.py` serves them over Streamable HTTP behind Clerk OAuth with a subject allowlist (`docs/runbooks/remote-mcp.md`) |
| `docs/runbooks/recovery.md`, `tests/test_recovery_rehearsal.py` | isolated PostgreSQL recovery, synthetic dump/restore acceptance and hosted recovery gates |
| `docs/runbooks/tasks-cutover-and-rollback.md` | historical SQLite cutover/retirement and runtime configuration guidance |
| `tests/` | isolation, RLS bypass, delegation, authorization conformance and TOCTOU, API, contract, writer parity, graph, MCP and recovery tests |

## Security model

- Every `platform` table has RLS enabled and forced. Rows are visible only when
  `app.workspace_id` (set with `SET LOCAL` from a verified token) matches.
  Without it, nothing is visible.
- `platform_api` cannot disable or alter RLS, assume the owner role, create
  objects, or change `audit_events`, which is also append-only for its owner.
- Agents hold no direct permissions. They act through a delegation whose scopes
  must be a subset of the grantor's permissions when created and are checked
  against the grantor's live permissions on every use.
- Acting on behalf of another identity requires a live delegation from that
  identity.
- Application code still authorizes every request; RLS is the second line of
  defence.

## Authorization

`platform.authorize(actor, workspace, action, resource, delegation, on_behalf_of,
approval, channel, lock)` is the only place that decides. It denies by default,
answers `resource_not_found` for other workspaces, follows delegation
revocation and the grantor's live permissions, applies approval policies, and
records every decision. Other languages call the same function; the contract
and its conformance cases live in `contracts/authz/v1/`.

Writes check twice: an unlocked, recorded precheck, then `enforce` (with row
locks) inside the transaction that changes data, so a revocation between the
two is caught.

## Token rotation

Automation tokens live 30 days. `empire-token-rotate` re-provisions the automations
whose token (local or deployed) expires within `rotate_within_days`, then pushes the
new tokens to every target in `contracts/automations/v1/deployments.json` (Railway
service variables, Vercel production env + one production deploy). Values move on
stdin only; output is JSON lines without secrets. It runs weekly from the owner's
crontab (Monday 02:30 Asia/Bangkok) next to the platform monitor:

```bash
EMPIRE_DB_MODE=hosted .venv/bin/empire-token-rotate --workspace owner \
  --tokens ~/.config/empire-platform/tokens --key-dir ~/.config/empire-platform/signing \
  --issuer empire-platform [--dry-run] [--force] [--only AUTOMATION]
```

Add a target to `deployments.json` whenever an automation token is deployed somewhere new.

Hosted run: the `token-rotate` Railway cron service (`empire-token-rotate-hosted`,
`src/empire_platform/hosted_rotate.py`, schedule in `.railway/railway.ts`). Deploys (CD, since
2026-10-18): `token-rotate` is connected to this repository's `main` with Railway's Wait for CI
(`checkSuites`). A merge to `main` deploys only after the CI and security checks pass; a
failing check leaves production on the previous deployment. Roll back by redeploying an earlier
deployment in Railway. Do not `railway up` it from a working tree; `railway config apply` is
for service settings only. `tasks-api`, `tasks-publisher`, `recurring-gen` and `empire-mcp` are
not connected yet and still deploy with `railway up --ci --service <name>`.
