# SPEC-001 — stripe-reconcile: refund and dispute adjustment rows

Status: proposed (2026-11-11). Upstream: sales-crm SPEC-001 (Stripe money exceptions), risk 1.

## Problem

Since sales-crm `f976d8b` the crm-income webhook writes signed adjustment rows
(`entry_type` `refund`, `dispute`, `dispute_fee`; `notes.source = "stripe-webhook-adjustment"`).
`stripe-reconcile` reads only `source = "stripe-webhook"` rows, so:

1. refund and dispute balance transactions in a payout are never tied to their ledger rows;
2. a dispute fee is counted twice in owner cash: once as the ledger `dispute_fee` row and once inside
   the payout `fee` transaction (which sums every balance transaction's fee), and the owner-cash
   fee check (Stripe fee transactions − ledger fees) shows a false difference.

## Decisions

- **Matching.** Payout balance transactions are listed with `expand[]=data.source`.
  - `refund` / `payment_refund`: the ledger `refund` row whose `notes.stripe.refund` is the refund id;
    otherwise (cumulative rows, the norm on API `2024-10-28.acacia` where `charge.refunds` is not in
    the webhook payload) an unused `refund` row of the same charge, preferring `refund_minor == −amount`.
  - Refunds are **allocated by amount** (`refund_minor`) against each refund row's unallocated balance,
    shared across payouts in a run and seeded from earlier reconciliations' `adjustment_allocations`, so one
    cumulative row can cover several refunds and two equal refunds in different payouts use two rows (review
    of #19, 2026-11-11).
  - dispute adjustments (`type = adjustment` with a dispute source, or `reporting_category`
    `dispute` / `dispute_reversal`): the `dispute` and `dispute_fee` rows whose
    `notes.stripe.balance_transaction` is the balance transaction id.
- **No ledger writes for adjustments.** crm-api locks the notes of adjustment rows
  (409 `adjustment_notes_locked`), so matches are recorded only in the reconciliation notes
  (`matched_adjustment_ids`, `unmatched_adjustment_balance_transactions`).
- **Dispute fee has one owner: the ledger.** The fee of a dispute balance transaction that matched a
  `dispute_fee` row is left out of the payout `fee` transaction and reported as
  `dispute_fees_in_ledger`. An unmatched dispute keeps its fee in the `fee` transaction so cash still
  adds up.
- **Status.** `matched` needs the payout to equal its balance transactions, every payment matched and
  every refund/dispute balance transaction matched; otherwise `exception`.

## Known limits

- Reconciliations written before this spec have no `adjustment_allocations`, so their refunds are not
  subtracted from the refund rows' balances. Not reachable on current data: crm-income had **no** refund
  rows before the money-exception webhook (2026-11-11), and no reconciliation has run since with refund
  rows (checked 2026-11-11: 10 ledger rows, all `revenue`). If an older reconciliation ever needs it,
  backfill `adjustment_allocations` in its notes by hand.

## Acceptance (tests)

- AC-1 refund + dispute rows of a payout are listed as matched; the fee transaction excludes the
  matched dispute fee; status `matched`; no ledger PATCH on adjustment rows.
- AC-2 a dispute balance transaction with no ledger row keeps its fee in the fee transaction and makes
  the reconciliation an `exception` naming it.
- AC-3 re-run writes nothing (existing idempotency).
- AC-4 two equal refunds of one charge in two payouts use two ledger rows.
- AC-5 one cumulative refund row covers two refund balance transactions in the same payout.
- AC-6 a refund settled in another currency is allocated by the refund's own amount (`source.amount`, the
  charge currency of `refund_minor`), not the settlement amount.
- AC-9 the stale-fee check also runs when the new fee is zero (the transaction is looked up instead of created).
- AC-8 a dispute balance transaction is matched only when each non-zero part (amount, fee) has its own
  ledger row (`dispute`, `dispute_fee`).
- AC-7 a fee transaction returned by its idempotency key with an amount other than the computed fee (an
  interrupted earlier run) makes the reconciliation an `exception` with `fee_transaction_mismatch`.
