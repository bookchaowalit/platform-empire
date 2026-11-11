# SPEC-001 stripe-reconcile: refund and dispute balance transactions

| Field | Value |
|---|---|
| Status | Draft (maker: Claude, 2026-11-11), waits for review by someone who did not write it + owner approval |
| Upstream | sales-crm SPEC-001 `specs/001-stripe-money-exceptions/spec.md` (bookchaowalit/sales-crm#12) |
| Code | `src/empire_platform/stripe_reconcile.py`, `tests/test_stripe_reconcile.py` |
| Runbook | `docs/runbooks/stripe-reconcile.md` |

## Problem

sales-crm#12 makes the crm-income Stripe webhook write signed adjusting ledger rows
(`entry_type` `refund`, `dispute`, `dispute_fee`, notes `source: stripe-webhook-adjustment`).
`stripe-reconcile` matches only payment balance transactions (`charge`, `payment`) to ledger
rows. So a payout with a refund or a dispute:

1. reconciles as `matched` although the refund/dispute is never checked against the ledger
   (a missing ledger adjustment goes unnoticed), and
2. puts the dispute fee into the payout `fee` transaction while the ledger also carries it as a
   `dispute_fee` row, so "ledger revenue − fee transactions" subtracts the dispute fee twice.

## Scope

In: matching refund and dispute balance transactions in a payout to ledger adjustment rows;
the dispute-fee split; reconciliation notes; runbook.
Out: writing to adjustment rows (crm-api refuses PATCH of their notes, 409
`adjustment_notes_locked`); `refund.failed` (no ledger entry exists upstream yet); other
balance transaction types (`stripe_fee`, `adjustment` that is not a dispute, …) stay unchecked
as today.

## Behaviour

| Id | Given | Then |
|---|---|---|
| AC-01 | ledger rows with notes `source: stripe-webhook-adjustment` | they are read, never treated as payments (no fee lookup, no `cash_reconciled` write) |
| AC-02 | a payout balance transaction of type `refund` / `payment_refund` whose `source` is refund `re_X`, and an adjustment row with `stripe.refund = re_X` | matched; the row id is in `matched_adjustment_ids` |
| AC-03 | as AC-02 but the only refund row of that charge is the fallback row (no `stripe.refund`, `stripe.charge = ch_Y`) and the refund's charge is `ch_Y` | matched to that row |
| AC-04 | a payout balance transaction of type `adjustment` with `reporting_category` `dispute` or `dispute_reversal`, and an adjustment row with `stripe.balance_transaction` = its id | matched |
| AC-05 | a refund or dispute balance transaction with no ledger adjustment row (or `refund_failure` / `payment_failure_refund`, which the ledger does not record) | listed in `unmatched_balance_transactions`; the reconciliation is `exception` |
| AC-06 | a matched dispute balance transaction with a fee, and its `dispute_fee` row exists | its fee is left out of the payout `fee` transaction and reported as `dispute_fees_in_ledger`; `expected_amount` (sum of nets) is unchanged |
| AC-07 | a dispute balance transaction whose fee has no `dispute_fee` row | its fee stays in the `fee` transaction (no money dropped) and the transaction is unmatched (AC-05) |
| AC-08 | a re-run | writes nothing (unchanged idempotency) |
| AC-09 | `--dry-run` | the preview shows the same matching and fee split, writes nothing |

Balance transactions are listed with `expand[]=data.source` so a refund's charge is known
without one extra call per refund.

## Why the fee split this way

The ledger is gross revenue and the transactions cell holds cash movements: payment fees live
only in the `fee` transaction. Upstream chose to put the dispute fee in the ledger (so the
ledger equals the money Stripe kept on a lost dispute). Keeping it in both places double counts,
so the fee transaction drops the fee that the ledger already holds. Payout cash still adds up:
transfer = payout = sum of nets.

## Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | `fee` transaction no longer equals Stripe's "fees" total in the dashboard | `dispute_fees_in_ledger` in reconciliation notes; runbook says so |
| 2 | A fallback refund row (no refund id) can stand for several refunds of one charge | allowed: matching is by charge; amounts are not compared (the ledger may hold the original's currency) |
| 3 | `expand[]=data.source` makes the balance transaction list heavier | one payout has few transactions; pages stay 100 |

## Red / green log

| AC | Test | Red | Green |
|---|---|---|---|
| AC-01..AC-09 | `tests/test_stripe_reconcile.py` (`test_refund_*`, `test_dispute_*`, `test_adjustment_rows_are_not_payments`) | d115e8d · `uv run --python 3.12 --extra dev pytest tests/test_stripe_reconcile.py` · `10 failed, 2 passed` (no `expand[]=data.source`, `KeyError: matched_adjustment_ids`) | 12 passed locally; CI run of the PR tip |

## Change log

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-11-11 | First draft |
