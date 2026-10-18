# Cell executions and automation tokens

Two daily-check signals from `empire-monitor` that need an owner action:
a cell execution left `running`, and an automation token close to expiry.
Never print, paste, or commit a token, a signing key, or a database URL;
every command below reads them from files or stdin.

## Stuck cell executions

A cell with its own database (for example `solo-decisions`) starts an
execution with `POST /v1/executions`, commits its own rows, then calls
`POST /v1/executions/{id}/finish`. If that finish call is lost (platform down,
network, crash after the local commit), the execution stays `running`.

| Signal | Warn | Breach |
|---|---|---|
| `cell_executions_stuck` (per cell service) | running > 15 min | running > 24 h |

The monitor reads only the service, status, and start time of each execution
(migration `services/monitor/migrations/3002_cell_executions.sql`).

### Reconcile

Run from this checkout against the hosted database (URLs in the owner-only
`~/.config/empire-platform/db.env`):

```bash
EMPIRE_DB_MODE=hosted .venv/bin/empire-cells reconcile --cell decisions
```

This lists executions of that cell running for more than 15 minutes: id,
start time, bound permission, and bound resource. For each one, check the
cell's own database (read-only, from the cell's checkout) for rows tagged with
the execution:

```sql
-- solo-decisions
SELECT type, subject FROM decisions.outbox WHERE execution_id = '<execution id>';
SELECT event_type FROM decisions.events WHERE execution_id = '<execution id>';
```

- **No rows**: the local transaction never committed. Close it as failed:

  ```bash
  EMPIRE_DB_MODE=hosted .venv/bin/empire-cells reconcile --cell decisions --execution <id> --fail
  ```

- **Rows exist**: the change is committed in the cell. Close it as succeeded
  and audit the action the cell recorded (the outbox `type`, for example
  `decisions.decision.updated`):

  ```bash
  EMPIRE_DB_MODE=hosted .venv/bin/empire-cells reconcile --cell decisions --execution <id> \
      --succeed --action decisions.decision.updated
  ```

Outcomes: `finished` (exit 0); `too_recent` (under 15 minutes, a live finish
may still arrive); `mismatch` (the action does not fit the bound operation);
`not_found` (other workspace or cell); or the existing status when the
execution was already closed.

### Why this cannot forge audit

`platform.reconcile_cell_execution` (migration
`services/platform/migrations/0007_reconcile_cell_executions.sql`) reads the
actor, delegation, on-behalf-of, service, and resource from the execution's
own binding and calls `platform.finish_cell_execution` with them. The
caller supplies only the outcome and the action. The same checks as a live
finish apply: the action must be in the bound service's namespace and name
the bound entity, and a `deleted` or `approved`/`rejected` verb needs the
matching bound right. The resource is always the bound one. A reconciled
success has no row hashes, and a second audit record
`platform.execution.reconciled` (service `platform`, resource the execution)
marks every reconciliation. Only `platform_api` may call the function, and
that role could already call `finish_cell_execution` directly.

The reconciliation record does not name who ran it (the CLI connects as
`platform_api`), and the platform cannot see the cell's rows: closing as
`--fail` when the cell did commit leaves the two out of step. Check the
cell's database first, every time.

## Automation token expiry

`empire-automations provision` mints one token per automation into
`~/.config/empire-platform/tokens/<automation>.jwt`. With `--ttl-days 30` they
expire 30 days after minting.

| Signal | Warn | Breach |
|---|---|---|
| `token_expiring` | expires within 7 days | — |
| `token_expired` | — | already expired |
| `token_unreadable` | file has no readable `exp` claim | — |

The monitor reads only the `exp` and `iat` claims (the signature is not
checked) and reports the file name, never the token. Tokens minted for one day
or less, such as `owner-cli` (one hour), expire by design and are skipped.
Override the directory with `--tokens DIR` or `EMPIRE_TOKENS_DIR`.

### Automatic rotation

The hosted `token-rotate` Railway cron (`empire-token-rotate-hosted`,
`src/empire_platform/hosted_rotate.py`) runs every Sunday 19:30 UTC and reports the
`token-rotate` heartbeat to the uptime monitor (alert after 8 days without a run).
`empire-token-rotate` (`src/empire_platform/rotate.py`) is the manual fallback from a
workstation; the owner's crontab is no longer used (retired 2026-10-18).
It re-provisions every automation whose local or deployed token expires within
10 days and pushes the new token on stdin to every target in
`contracts/automations/v1/deployments.json` (Railway service variables, Vercel
production variables). With a weekly run and a 10-day window, a
`token_expiring` (7 days) or `token_expired` finding means rotation failed:
read the log, then run it by hand:

```bash
EMPIRE_DB_MODE=hosted .venv/bin/empire-token-rotate --workspace owner \
    --tokens ~/.config/empire-platform/tokens --key-dir ~/.config/empire-platform/signing \
    --issuer empire-platform --dry-run   # then without --dry-run
```

When an automation token is deployed somewhere new, add a target to
`deployments.json`; a copy that is not listed there is never rotated.

### Manual rotation (fallback)

1. Mint new tokens (idempotent; it also follows scope changes in
   `contracts/automations/v1/automations.json`):

   ```bash
   EMPIRE_DB_MODE=hosted .venv/bin/empire-automations provision --workspace owner \
       --tokens ~/.config/empire-platform/tokens --key-dir ~/.config/empire-platform/signing \
       --issuer empire-platform --ttl-days 30
   ```

   Local automations read the files directly and pick up the new tokens on
   their next run.

2. Update every copy outside the token directory. Example: Railway
   project `solo-decisions`, variable `DECISIONS_PUBLISHER_TOKEN` on services
   `decisions-api` and `decisions-publisher`. From the linked `solo-decisions`
   checkout, pass the value on stdin so it never appears in the shell history
   or the process list:

   ```bash
   railway variable set DECISIONS_PUBLISHER_TOKEN --stdin --service decisions-api \
       < ~/.config/empire-platform/tokens/decisions-publisher.jwt
   railway variable set DECISIONS_PUBLISHER_TOKEN --stdin --service decisions-publisher \
       < ~/.config/empire-platform/tokens/decisions-publisher.jwt
   ```

   Each set redeploys the service. The full list of copies is
   `contracts/automations/v1/deployments.json`; update each of them the same way.

3. Verify: `empire-monitor --json` reports no `token_*` finding, both Railway
   services are healthy (`railway service status`), and the decisions outbox
   drains (no `401` in `railway logs --service decisions-publisher`).

The old tokens stay valid until their own `exp`; rotation does not revoke
them. To cut one off at once, revoke the automation's delegation.
