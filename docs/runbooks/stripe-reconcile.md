# Stripe reconcile (`stripe-reconcile`)

Gap: architecture-docs `docs/architecture/EOS-GAP-MAP.md` G2 (minimal Order-to-Cash).
Provider decision: business-catalog `docs/commercial/erp/PAYMENT-PROVIDERS-2026-10.md`.
Upstream: sales-crm `services/crm-api/stripe_webhook.py` writes each paid Stripe payment
as gross revenue in the crm-income ledger.

## What it does

| Step | Reads | Writes | Idempotent on |
|---|---|---|---|
| Fees | ledger rows with `notes.source = stripe-webhook` and no fee; the Stripe balance transaction of each payment | row notes: `fee`, `net`, `settlement_currency`, `balance_transaction` | rows that already have a fee are skipped |
| Accounts | finance-accounts (`owner_scope = solo_empire`) | `Stripe balance` (type `payment`) and the payout bank account `<bank> ••<last4>` (type `bank`), once | name + type; `Idempotency-Key` |
| Payouts | paid Stripe payouts of the last 90 days and their balance transactions; reconciliations with `source_type = wallet_export` | one `transfer` transaction (payout, bank account, arrival date), one `fee` transaction (Stripe fees, Stripe account), ledger notes `cash_reconciled: true` + `payout`, then one reconciliation (`source_ref` = payout id) | the reconciliation is the done marker; every create sends `Idempotency-Key: stripe:<payout>:<purpose>` |

A reconciliation is `matched` when the payout equals the sum of its balance transactions and
every payment in it has a ledger row; otherwise `exception`, with the unmatched balance
transactions in its notes (for example the ฿10 readiness test paid before the webhook existed).

Revenue stays in the ledger and is not copied to transactions: transactions hold only cash
movements (payout transfer, fees), so owner-cash reporting does not count income twice.

## Run it

```bash
empire-stripe-reconcile --dry-run                              # reads everything, writes nothing
empire-stripe-reconcile --dry-run --payout-status in_transit   # preview a payout before it arrives
empire-stripe-reconcile                                        # write
```

Output: one JSON line per item (`stripe_reconcile.fee_recorded`, `.would_reconcile`,
`.payout_reconciled`, `.payout_unchanged`, `.error`, …) and a final `stripe_reconcile.run` line.
Exit 1 when an item failed (the rest still ran), 2 when configuration is missing.

## Variables (Railway service only, values from Infisical, never printed)

| Variable | Source |
|---|---|
| `EMPIRE_SERVICE=stripe-reconcile` | literal |
| `STRIPE_SECRET_KEY` | Infisical `/stripe` |
| `CRM_LEDGER_URL` | `https://crm-api-production-19fd.up.railway.app` (crm-income) |
| `CRM_LEDGER_TOKEN` | Infisical `/income` `CRM_API_TOKEN` |
| `EMPIRE_FINANCE_ACCOUNTS_API_URL`, `EMPIRE_TRANSACTIONS_API_URL`, `EMPIRE_RECONCILIATIONS_API_URL` | the cells' Railway URLs (solo-empire `online-systems.yaml`) |
| `STRIPE_RECONCILER_TOKEN` | automation `stripe-reconciler`, renewed by `token-rotate` (target in `deployments.json`) |
| `MONITOR_URL`, `HEARTBEAT_TOKEN` | Infisical `/monitoring` |
| `SENTRY_DSN` | as the other services |

Schedule: `30 2 * * *` UTC (09:30 Bangkok), after Stripe's Monday payouts land.

## State (2026-10-25, live)

- Code on `main` (`82772a7`, CI green): job, tests, automation `stripe-reconciler` in
  `automations.json`, both token targets in `deployments.json`, `.railway/railway.ts` service.
- Automation `stripe-reconciler` provisioned on the hosted `owner` workspace (owner-approved
  2026-10-25) with `hosted_rotate.provision_mint` under the `token-rotate` service environment,
  only this automation (no full rotation). Token expires 2026-11-24; the weekly `token-rotate`
  renews it. Stored in Infisical `/empire-platform` as `EMPIRE_AUTOMATION_TOKEN_STRIPE_RECONCILER`
  (written with the token-rotate machine identity) and on the Railway service as
  `STRIPE_RECONCILER_TOKEN`.
- Railway service `stripe-reconcile` (id `4104b44f-0516-4786-a887-5a3b7c370465`): start command
  `empire-stripe-reconcile`, restart `NEVER`, cron `30 2 * * *`; deployed from a clean
  `git archive` of `82772a7` (no source connection, so a push does not redeploy).
- Dry runs with the service variables: `paid` → nothing to do, 0 errors;
  `--payout-status in_transit` → `po_1USgOuKKW2mNhAm6ecpay0kU` ฿9.82 would reconcile as
  `exception`: amount equals its balance transactions, but the ฿10 readiness payment
  (`txn_3UNxnZKKW2mNhAm60uf4gdPz`, before the webhook existed) has no ledger row. Expected.
- First scheduled write: 2026-10-26 02:30 UTC, after that payout lands.
- Open: heartbeat in platform-uptime-worker (step 4) after the first good scheduled run.

## Turn on

1. Provision `stripe-reconciler` on the hosted `owner` workspace the same way as
   `backlog-sync` (`hosted_rotate.provision_mint` under the `token-rotate` environment); store
   the token in Infisical `/empire-platform` as `EMPIRE_AUTOMATION_TOKEN_STRIPE_RECONCILER`.
2. Railway service `stripe-reconcile`: set `STRIPE_RECONCILER_TOKEN`, start command
   `empire-stripe-reconcile`, restart `NEVER`, cron `30 2 * * *`, then deploy CI-green `main`
   (`railway up` of a clean `git archive`, like `backlog-sync`, until the source is connected).
3. `railway run --service stripe-reconcile -- empire-stripe-reconcile --dry-run`, read it, then let
   the cron write.
4. Add the heartbeat `{ job: 'stripe-reconcile', maxAgeHours: 26 }` to platform-uptime-worker
   after the first good run.

Rollback: delete the Railway service; records it wrote stay (transactions and reconciliations
carry `source = stripe` / `source_ref = po_…`), and the token lapses within 30 days or is revoked
with its delegation.
