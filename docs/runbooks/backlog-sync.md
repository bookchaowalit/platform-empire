# Backlog sync (`backlog-sync`)

Decision: architecture-docs `docs/architecture/decisions/ADR-0006-backlog-sync-host.md`.
Policy: architecture-docs `operations/policies/TASK-SOURCE-OF-TRUTH.md` (D2, 2026-10-23).
Keys and labels: architecture-docs `docs/lifecycle/OUTCOME-REVIEW-TO-BACKLOG.md`.

## What it does

| Source | Read | Task key | Fields it owns |
|---|---|---|---|
| GitHub | open issues, and issues closed in the last 30 days, labelled `autopilot` or `backlog`, in the owners listed in `BACKLOG_SYNC_GITHUB_OWNERS`, opened by an owner, member, collaborator or the repository's own `github-actions[bot]` (other authors are skipped: anyone can edit their own title or close it) | `github/<owner>/<repo>#<n>` | title, URL, labels as tags, project, status (open → `todo`, `autopilot:doing` → `in_progress`, `autopilot:blocked` → `blocked`, closed completed → `done`, closed not planned → `cancelled` + archived) |
| Sentry | unresolved `error`/`fatal` issues in `SENTRY_ORG` seen in the last 14 days | `sentry/<project>/<issue id>` | title (exception type and a cleaned location only, never the message), sent with If-Match, URL, description; a `regressed` issue reopens a done task |
| Files | Markdown found by GitHub code search: outcome reviews (`Review ID` + `Decisions and actions`, status `decided`) and incident reviews (`Incident ID/severity`); `templates/` and `status: template` files are skipped | `outcome-review/<OR-id>#<row>`, `incident/<INC-id>#<row>` | title, file URL, type tags, description; incident status from `done…` |

Priority (labels `P0`–`P3` → 1–4; Sentry fatal 2, error 3) and due date are sent only
when the task is created; after that they belong to tasks-api.

## Run it

```bash
# dry run: reads the sources (and tasks-api if a token is set); writes nothing
empire-backlog-sync --dry-run
empire-backlog-sync --dry-run --sources github
```

Output is one JSON line per item (`backlog.would_create`, `backlog.created`,
`backlog.unchanged`, …) and a final `backlog.run` line with counts. Exit 1 when any
source or item failed (the rest still ran), 2 when configuration is missing.

Local staging (what was run on 2026-10-23 before writes were enabled):
`empire-db start`, create a workspace, `empire-automations provision --workspace
staging --only backlog-sync --tokens DIR`, `tasks-api`, then run the sync with
`EMPIRE_TASKS_API_URL=http://127.0.0.1:3601` and `BACKLOG_SYNC_TOKEN` from DIR.
Result: dry run 11 `would_create` → write 11 `created` → re-run 11 `unchanged`.

## Variables (Railway service only, values from Infisical, never printed)

| Variable | Source |
|---|---|
| `EMPIRE_SERVICE=backlog-sync` | literal |
| `EMPIRE_TASKS_API_URL` | same as `recurring-gen` |
| `BACKLOG_SYNC_TOKEN` | minted for automation `backlog-sync` (owner action), then renewed by `token-rotate` once the target below is in `contracts/automations/v1/deployments.json` |
| `BACKLOG_SYNC_GITHUB_TOKEN` | fine-grained PAT, read-only Issues + Contents + Metadata on every owner org (owner action) |
| `BACKLOG_SYNC_GITHUB_OWNERS` | `bookchaowalit` plus every `bookchaowalit-*` org; never `Happy-Teeth` |
| `SENTRY_AUTH_TOKEN` | Infisical `/sentry` (reads issues; checked 2026-10-23) |
| `SENTRY_ORG` | `personal-yu0` |
| `MONITOR_URL`, `HEARTBEAT_TOKEN` | Infisical `/monitoring` |
| `SENTRY_DSN` | as the other services |

## Turn on (owner actions, in order)

1. Deploy `main` of platform-empire to `tasks-api` (the `/v1/tasks/external/{source}/{external_id:path}`
   fix, commit `8994be7`). Until then GitHub keys (`github/<owner>/<repo>#<n>`) return 404 on the
   live API; Sentry keys work today. Railway Wait for CI is stuck, so this deploy waits for the owner.
2. Provision the automation on the hosted workspace:
   `empire-automations provision --workspace <slug> --only backlog-sync --tokens <dir>` with the
   production key (as for the other automations), and put the token in Infisical `/empire-platform`
   as `EMPIRE_AUTOMATION_TOKEN_BACKLOG_SYNC`.
3. Create the fine-grained GitHub PAT above, store it in Infisical `/github` as
   `BACKLOG_SYNC_GITHUB_TOKEN`.
4. Create the Railway cron service (same image as `recurring-gen`); add it to `.railway/railway.ts`:

   ```ts
   // Hourly (EMPIRE_SERVICE=backlog-sync): GitHub issues, Sentry errors, outcome reviews and
   // incidents -> one task each (empire_platform.backlog_sync). 5 * * * * UTC.
   const backlogSync = service("backlog-sync", {
     source: github,
     startCommand: "empire-backlog-sync",
     deploy: { cronSchedule: "5 * * * *", restartPolicyType: "NEVER" },
   });
   ```

   and add `{ "automation": "backlog-sync", "provider": "railway", "checkout": "platform-empire",
   "service": "backlog-sync", "variable": "BACKLOG_SYNC_TOKEN" }` to `deployments.json`.
5. First hosted run: `railway run --service backlog-sync -- empire-backlog-sync --dry-run`, read the
   output, then let the cron run write.
6. Add the heartbeat `{ job: 'backlog-sync', maxAgeHours: 3 }` to platform-uptime-worker
   `src/targets.ts` only after the first good run (otherwise it alerts stale at once).
