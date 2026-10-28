# Tasks cutover and rollback history

For current PostgreSQL/Neon recovery use [recovery.md](recovery.md). The
cutover/retirement entries below are historical reports from 2026-10-09,
not a current hosted inventory or a verified restore. They report production
cutover, SQLite retirement and an owner-waived rollback window. Current hosted
snapshot availability, retention and production recovery remain unverified
until an isolated Neon drill produces evidence.

The SQLite flow and commands below are historical only. Do not recreate the
retired importer or switch production back to SQLite. Runtime configuration
is guidance, not proof of an active deployment. Legacy functions retained in
`src/empire_platform/cutover.py` are not a supported hosted recovery interface.

Source of truth is exactly one side at a time. There is never a dual write:
SQLite is frozen while PostgreSQL accepts writes, and the Tasks API write
switch is off while SQLite accepts writes again.

## Historical cutover

| Step | Command / mechanism | Rehearsed by |
|---|---|---|
| 1. Back up both sides | copy the SQLite database file; `cutover.backup()` (pg_dump) | `test_restore_from_backup_recovers_schema_and_data` |
| 2. Freeze SQLite tasks writers | `cutover.freeze_sqlite()` installs refusing triggers on the tasks tables | `test_frozen_sqlite_refuses_writers_until_rollback` |
| 3. Final import | one-off SQLite import (verified counts, references, and every field; the importer was removed after the cutover finished) | done 2026-10-09 |
| 3b. Seed the task-sync ledger | `SOLO_EMPIRE_TASKS_BACKEND=api task-sync seed-ledger` copies each task's SQLite sync state (local, synced, dirty, conflict, hash) into `task_sync_ledger`, so the first push after cutover sends what SQLite would have sent; refuses to run twice | `scripts/rehearse_cutover.py` (88 of 88 on current data) |
| 4. Writers switch to the API | `SOLO_EMPIRE_TASKS_BACKEND=api` plus one token per automation (`empire-automations provision --workspace owner --tokens DIR`) | `scripts/rehearse_cutover.py` on a copy of current data; Go and TypeScript writers still pending |
| 5. API writes on | `cutover.set_writes(True)` | `test_write_switch_stops_api_writes_but_not_reads` |

## Historical rollback

| Step | Command / mechanism | Rehearsed by |
|---|---|---|
| 1. API writes off | `cutover.set_writes(False)`; the API answers 503 for changes and waits for writes already under way | `test_write_switch_stops_api_writes_but_not_reads` |
| 2. Back up PostgreSQL | `cutover.backup()` for forensics | `test_restore_from_backup_recovers_schema_and_data` |
| 3. Export to SQLite | `cutover.export_to_sqlite()`: updates, new tasks (new SQLite IDs, parents resolved), deletions, and new links to legacy targets, in one SQLite transaction | `test_full_rollback_keeps_every_write_made_after_cutover`, `test_export_is_all_or_nothing` |
| 4. Review unmapped links | links to entities that only exist in the new system are listed in `ExportReport.links_unmapped` and stay in the PostgreSQL backup | `test_full_rollback_keeps_every_write_made_after_cutover` |
| 5. Unfreeze SQLite | `cutover.unfreeze_sqlite()`; writers go back to SQLite | same |

## Daily check and rollback signals

`empire-monitor --api <tasks-api-url>` runs as the `platform_monitor` role, which
can read identifiers, counts, versions, outbox state, and decision codes only.
It compares each run with the previous one (`.runtime/monitor/state.json`) and
reports `ok`, `warn`, or `breach`:

| Signal | Warn | Breach (rollback criterion) |
|---|---|---|
| Tasks lost (fewer tasks than before beyond recorded deletions) | — | any |
| Duplicate task numbers in a workspace | — | any |
| Outbox oldest unpublished event | > 15 min | > 60 min |
| Outbox event retried | ≥ 3 times | — |
| Graph not applying published events | > 60 min | — |
| Tasks API health check failing | at once | > 30 min |
| Write switch off | yes | — |
| Cross-workspace attempts denied in 24 h | any | — |
| Cell execution still running (lost finish call) | > 15 min | > 24 h |
| Automation token in `~/.config/empire-platform/tokens` | expires within 7 days | expired |

The historical operator wrapper ran the check and sent alerts through the
configured channel. A breach needs diagnosis and an owner decision; it does
not authorize an external message, destructive restore or production switch.
The cell-execution and token signals are not rollback criteria; reconcile or
rotate per `docs/runbooks/cell-executions-and-tokens.md`.
Not covered yet: the Tasks API 5xx rate needs request metrics from the
hosted runtime.

## Historical rehearsal on a copy of data

The rehearsal on a copy of current data (cutover, writer operations, rollback) was run before the 2026-10-09 cutover; the rehearsal script and the SQLite importer were removed afterwards, together with their rollback tests. The "Rehearsed by" test names in the tables above are history.

## Schema or data damage without leaving PostgreSQL

Use the isolated restore workflow and executable synthetic acceptance test in
[recovery.md](recovery.md). A passing local drill does not verify hosted Neon
restore or authorize pointing production services at a recovered database.

## Hosted deployment

The Tasks API and the outbox publisher support Railway deployment against Neon.
Current deployment state must be verified separately. One image (`Dockerfile`) serves both; `EMPIRE_SERVICE` picks the
process. Variables are listed by name only; values live in Railway variables
and `~/.config/empire-platform/db.env`, never in the repo.

| Railway service | Config file | Variables |
|---|---|---|
| `tasks-api` | `.railway/railway.ts` (healthcheck `/health`) | `EMPIRE_SERVICE=api`, `EMPIRE_DB_MODE=hosted`, `EMPIRE_DB_URL_TASKS_API`, `TASKS_API_JWT_PUBLIC_KEY_PEM`, `TASKS_API_JWT_ISSUER`, `TASKS_API_HOST=0.0.0.0` |
| `tasks-publisher` | `.railway/railway.ts` (hourly cron `5 * * * *`, `empire-publish --batch 1000`, since 2026-10-28) | `EMPIRE_SERVICE=publisher`, `EMPIRE_DB_MODE=hosted`, `EMPIRE_DB_URL_TASKS_PUBLISHER`, `EMPIRE_DB_URL_GRAPH_API`; `PUBLISH_INTERVAL` only applies to the old loop mode |
| `recurring-gen` | `.railway/railway.ts` (cron `15 17 * * *` UTC = 00:15 Asia/Bangkok, no healthcheck, start command `empire-recurring`) | `EMPIRE_SERVICE=recurring`, `EMPIRE_TASKS_API_URL`, `RECURRING_GEN_TOKEN` (the `recurring-gen` automation token: `tasks.task:read`, `tasks.occurrence:write`; rotate with the other automation tokens) |
| `token-rotate` | `.railway/railway.ts` (cron `30 19 * * 0` UTC, no healthcheck, start command `empire-token-rotate-hosted`) | see the README section *Token rotation* (`EMPIRE_SIGNING_PRIVATE_KEY_PEM`, provider tokens, `MONITOR_URL`, `HEARTBEAT_TOKEN`) |

The hosted `empire-mcp` service was retired on 2026-10-18 (never deployed; see [`remote-mcp.md`](remote-mcp.md)). Every service above is declared with its GitHub source (`main`, Wait for CI), so `railway config apply` keeps CD connected.

- Railway sets `PORT`; the API honors it before `TASKS_API_PORT`.
- Railway Infrastructure as Code (`.railway/railway.ts`, partial
  `empire-platform`) declares both services; variables stay unmanaged, so no
  secret is in the file. Preview with `railway config plan`, apply with
  `railway config apply` (run `railway` directly, not through `timeout`: the
  SDK finds the CLI through `$_`). Config as Code (`railway.json`) was
  retired on 2026-10-10.
- With `PORT` set, the publisher answers `/health` (200 while its loop runs,
  503 once it stops). No start-command override is needed because the
  entrypoint reads `EMPIRE_SERVICE`.
- In hosted mode the API refuses to start without `TASKS_API_JWT_PUBLIC_KEY_PEM`
  or with the dev issuer `empire-platform-dev`, or with the dev public key.
  Tokens WITHOUT a delegation (`del` claim, i.e. owner tokens) are limited to
  3600 s in every mode. Tokens WITH a delegation may live up to 30 days
  (`TASKS_API_JWT_MAX_LIFETIME_SECONDS` overrides, hard cap 30 days); that is
  acceptable because every call re-checks the delegation's `revoked_at` and
  expiry, so revocation applies immediately.
- The publisher handles SIGTERM, logs one JSON line of counts per batch, and
  backs off on database errors instead of exiting.

Signing keys. Generate once on the operator machine, never on Railway:

```bash
empire-automations keys --dir ~/.config/empire-platform/signing   # prints the public key path
empire-automations provision --workspace SLUG --tokens ~/.config/empire-platform/tokens \
  --key-dir ~/.config/empire-platform/signing --issuer ISSUER --ttl-days 30
```

`provision` also creates the `owner-cli` automation (scopes `tasks.task:read`,
`tasks.task:write`, `tasks.status:write`). `infra/cli.ts` and
`infra/scripts/cli/decision-cli.py` use that delegated `owner-cli` token like
every other writer, so revoking its delegation cuts them off immediately.
`owner-token` (no delegation, at most 1 hour) is for short manual sessions only:

```bash
empire-automations owner-token --workspace SLUG --name owner-manual \
  --tokens ~/.config/empire-platform/tokens --key-dir ~/.config/empire-platform/signing --issuer ISSUER
```

Clients read tokens from `<repo>/.runtime/automation-tokens/<name>.jwt`, then
`~/.config/empire-platform/tokens/<name>.jwt`; a token file readable by group or
others is refused. Clients also refuse a non-https API URL unless the host is
loopback.

The private key (`private.pem`, mode 0600 in a 0700 directory) stays in
`~/.config/empire-platform/signing`; only the contents of `public.pem` go into
`TASKS_API_JWT_PUBLIC_KEY_PEM`. `keys` refuses to overwrite existing keys.
Rotating means a new key pair, a new public key variable, and re-minted tokens.

## Known limits

- Export covers tasks, task links, and the task hierarchy. Assignments, role
  suggestions, and types are not changed after cutover by any API today, so
  SQLite keeps its own copies.
- Execution, audit, and authorization records exist only in PostgreSQL; keep
  the backup from rollback step 2.

## Historical production cutover log (2026-10-09; not reverified)

| Step | Result |
|---|---|
| Neon | project `empire-platform` (aws-ap-southeast-1, PG 16), database `empire`, 14 migrations, runtime roles verified (no elevated attributes or memberships) |
| Railway | project `empire-platform`, services `tasks-api` (`/health` 200, 401 without a token) and `tasks-publisher` |
| Backup | Reported SQLite copy and Neon snapshot branch `cutover-after-import-<timestamp>`; present availability is unverified. The old bundled-client TLS claim is not a current capability check; see `recovery.md` |
| Freeze, import | SQLite task tables frozen; import 88/88 tasks, every table count equal, 0 field mismatches |
| Identities | 22 automations with delegations and 30-day tokens in `~/.config/empire-platform/tokens` |
| task-sync ledger | seeded 88/88 |
| Read parity | every TaskStore read method equal between frozen SQLite and the production API |
| Switch | `tasks_backend.py use-api --url https://tasks-api-production-4785.up.railway.app` |
| Smoke | create through `owner-cli` ok; archive refused for `owner-cli` (no archive scope) and done with a one-hour owner token; publisher delivered the events; monitor ok |
| Monitor | daily at 02:17 local time from the owner's crontab, alerts on Telegram |

Incident during cutover: the first `pg_dump` attempt failed and its traceback
printed the migrator connection URL. The migrator password was rotated
through the Neon API at once (old password rejected), and `backup`/`restore`
errors were subsequently described as redacted. The old regression test
`tests/test_cutover_rollback.py` was removed; do not treat that historical
claim as current security evidence. Current drills accept no credential URLs.

Historical rollback during the window: `tasks_backend.py use-sqlite` stopped clients from
using the API, then follow "Rollback" above (API writes off, export to SQLite,
unfreeze).

## Historical retirement (2026-10-09; not reverified)

| Step | Result |
|---|---|
| Final archive | SQLite copy with all 88 tasks in `~/.local/share/empire-platform/cutover/` (owner-only) |
| Adapters | the SQLite backend was removed from the Python, TypeScript, and both Go adapters; it survives only as a test double; `tasks_backend.py use-sqlite` was removed |
| Live SQLite | migration 080 rebuilt the ten tables that referenced `tasks(id)` without that foreign key (every row kept: 134, 70, and 5 linked rows) and dropped the eleven task tables; integrity ok, no foreign-key violations |
| Dependencies | `task_dependencies` moved to `GET /v1/task-dependencies` and `PUT /v1/tasks/{id}/dependencies` |
| Tests | `task test` runs with `EMPIRE_CLIENT_CONFIG` pointed at a missing file, so no test can reach the production API |

Incident during retirement: before that test isolation existed, test runs used
the production switch for about ten minutes and wrote to production (63 test
tasks, due dates of 2 tasks, completion of 1 task, one role suggestion). Every
change was found from the outbox and execution log and reverted through the
API (deletes with an owner approval, field restores from the archive). A full
comparison then showed production equal to the archived state except for the
archived smoke-test task 89.

Current recovery: validate a verified Neon point in time or snapshot in an
isolated branch, following [recovery.md](recovery.md). The archived SQLite task
copy does not contain PostgreSQL identities, authorization, audit, executions,
graph or outbox state and is not the platform recovery mechanism.
