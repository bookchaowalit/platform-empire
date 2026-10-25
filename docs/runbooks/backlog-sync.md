# Backlog sync (`backlog-sync`)

Decision: architecture-docs `docs/architecture/decisions/ADR-0006-backlog-sync-host.md`.
Policy: architecture-docs `operations/policies/TASK-SOURCE-OF-TRUTH.md` (D2, 2026-10-23).
Keys and labels: architecture-docs `docs/lifecycle/OUTCOME-REVIEW-TO-BACKLOG.md`.

## What it does

| Source | Read | Task key | Fields it owns |
|---|---|---|---|
| GitHub | open issues, and issues closed in the last 30 days, labelled `autopilot` or `backlog`, in the owners listed in `BACKLOG_SYNC_GITHUB_OWNERS`, opened by an owner, member, collaborator or `github-actions[bot]` in an autopilot repository listed in `BACKLOG_SYNC_BOT_REPOS` (other authors are skipped: anyone can edit their own title or close it) | `github/<owner>/<repo>#<n>` | title, URL, labels as tags, project, status (open → `todo`, `autopilot:doing` → `in_progress`, `autopilot:blocked` → `blocked`, closed completed → `done`, closed not planned → `cancelled` + archived) |
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
| `BACKLOG_SYNC_BOT_REPOS` | repos that run the autopilot (default `bookchaowalit-website/productivity-tracking-web`); add one when Wave 6 adds a repo |
| `SENTRY_AUTH_TOKEN` | Infisical `/sentry` (reads issues; checked 2026-10-23) |
| `SENTRY_ORG` | `personal-yu0` |
| `MONITOR_URL`, `HEARTBEAT_TOKEN` | Infisical `/monitoring` |
| `SENTRY_DSN` | as the other services |

## State (2026-10-24, session oa-remainder)

- Automation `backlog-sync` provisioned on the hosted `owner` workspace (scopes `tasks.task:read`,
  `tasks.task:write`, `tasks.archive:write`) with `hosted_rotate.provision_mint` run under the
  `token-rotate` service environment; the token is in Infisical `/empire-platform` as
  `EMPIRE_AUTOMATION_TOKEN_BACKLOG_SYNC` and on the Railway service as `BACKLOG_SYNC_TOKEN`.
  `reporting-reader` (for `task diary`) minted the same way: `EMPIRE_AUTOMATION_TOKEN_REPORTING_READER`.
  Both are `deployments.json` targets, so the weekly `token-rotate` renews them.
- Railway service `backlog-sync` (project `platform-empire`) created through the API to match
  `.railway/railway.ts`: cron `5 * * * *`, restart `NEVER`, start command
  `empire-backlog-sync --sources sentry`. Variables as in the table, except
  `BACKLOG_SYNC_GITHUB_TOKEN` (set 2026-10-25).
- **Sources (2026-10-24, superseded by the 2026-10-25 entry below):** Sentry was live; GitHub and Files were off: both need `BACKLOG_SYNC_GITHUB_TOKEN`,
  and the only GitHub token in Infisical (`/github` `GITHUB_PERSONAL_ACCESS_TOKEN`) is a classic PAT
  with admin scopes, which this job must not use. After the owner stores the read-only
  fine-grained PAT (step 3), set it on the service, run
  `railway run --service backlog-sync -- empire-backlog-sync --dry-run --sources github,files`,
  then drop `--sources sentry` from the start command.
- **Source connection:** `serviceConnect` to `bookchaowalit/platform-empire` is refused ("User does
  not have access to the repo") for both the owner CLI session and the Infisical Railway token, so
  the service runs a `railway up` of a clean `git archive` of CI-green `main` `002c165` and does
  not redeploy on push. Owner action: Railway → service `backlog-sync` → Settings → Source →
  connect `bookchaowalit/platform-empire` `main` (re-authorise the Railway GitHub app if asked)
  and enable Wait for CI.
- Runs: dry run 10 `would_create` (Sentry), live 10 `created`, re-run 10 `unchanged`, 0 errors.
- **2026-10-25: all sources on.** The owner stored a fine-grained, read-only PAT (code, issues,
  metadata; resource owner `bookchaowalit`, with expiry) as Infisical `/github`
  `BACKLOG_SYNC_GITHUB_TOKEN`; it is set on the service and the start command is now
  `empire-backlog-sync` (redeployed from CI-green `main` `1290e78`). Dry run `--sources github,files`:
  14 `would_create`, 0 errors. Live: 14 GitHub issues `created`, Sentry 11 `unchanged`, files 0;
  re-run 25 `unchanged`. Files 0 is real: the three `OUTCOME-REVIEW-001.md` are `draft`, the other
  code-search hits are templates, this runbook, or incident reviews with no decided actions.
- **Reach of the PAT:** every public repo in the 12 `bookchaowalit-*` orgs and all repos of the
  personal account (private included) are readable. **Private repos in the orgs return 404**
  (`bookchaowalit-freelance` 12, `bookchaowalit-agency` 2, `bookchaowalit-ai` 2 and one `*-hub` repo in
  each other org): a fine-grained PAT owned by the user cannot read org-private repos, so their issues
  are not synced. To include them, create a PAT with each org as resource owner (the org must allow
  fine-grained tokens) or install a GitHub App on the orgs.
- Rollback: delete the Railway service `backlog-sync` (the tasks stay in tasks-api), or remove
  `tasks.task:write` by revoking the delegation (`platform.delegations.revoked_at`).

## Turn on (owner actions, in order)

1. Done 2026-10-23: `tasks-api` runs `main` (`9a33ea7`, Railway deployed it after CI), so the
   `/v1/tasks/external/{source}/{external_id:path}` fix (`8994be7`) is live: a GitHub key such as
   `github/<owner>/<repo>%23<n>` reaches the route (401 without a token, not 404).
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
