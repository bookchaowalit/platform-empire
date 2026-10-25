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

## Turn on

1. Provision `stripe-reconciler` on the hosted `owner` workspace the same way as
   `backlog-sync` (`hosted_rotate.provision_mint` under the `token-rotate` environment); store
   the token in Infisical `/empire-platform` as `EMPIRE_AUTOMATION_TOKEN_STRIPE_RECONCILER`.
2. Create the Railway cron service `stripe-reconcile` in project `platform-empire` to match
   `.railway/railway.ts`, set the variables above, deploy CI-green `main`.
3. `railway run --service stripe-reconcile -- empire-stripe-reconcile --dry-run`, read it, then let
   the cron write.
4. Add the heartbeat `{ job: 'stripe-reconcile', maxAgeHours: 26 }` to platform-uptime-worker
   after the first good run.

Rollback: delete the Railway service; records it wrote stay (transactions and reconciliations
carry `source = stripe` / `source_ref = po_…`), and the token lapses within 30 days or is revoked
with its delegation.
