"""Stripe fees and payouts -> crm-income ledger, transactions and reconciliations (EOS gap G2).

The crm-api Stripe webhook records each paid payment as gross ledger revenue
(``notes.source = "stripe-webhook"``, ``fee``/``net`` empty, ``cash_reconciled`` false).
This job finishes the Order-to-Cash trail:

1. **Fees.** For every such revenue row without a fee, read the Stripe balance
   transaction of its payment and write ``fee``, ``net``, ``settlement_currency`` and
   ``balance_transaction`` into the row's notes.
2. **Payouts.** For every paid Stripe payout that has no reconciliation yet (looked up by
   ``source_ref = <payout id>``):

   - ensure two finance accounts (``Stripe balance``, type ``payment``; the payout bank
     account, type ``bank``; both ``owner_scope = solo_empire``);
   - create a ``transfer`` transaction (Stripe -> bank, the payout amount, on the arrival
     date) and one ``fee`` transaction (the Stripe fees inside the payout);
   - mark the ledger rows whose balance transaction is in the payout ``cash_reconciled``;
   - create the reconciliation last: ``expected_amount`` is the sum of the payout's balance
     transactions, ``reconciled_amount`` the payout; ``matched`` when they agree and every
     payment in it has a ledger row, otherwise ``exception`` with the unmatched ids in notes.

Revenue stays in the ledger; transactions hold only cash movements (payout transfer and
fees), so owner-cash reporting does not count income twice. Every write is idempotent
(cell ``Idempotency-Key`` = ``stripe:<object id>:<purpose>``; the reconciliation is the
done marker), so a re-run writes nothing.

    empire-stripe-reconcile [--dry-run] [--days 90] [--payout-status paid]

``--dry-run`` reads Stripe, the ledger and the cells and prints what it would write.
``--payout-status in_transit`` previews payouts that have not arrived (dry run only).
Exit 1 when an item failed (the rest still ran), 2 when configuration is missing.

Environment (nothing secret is printed):
    STRIPE_SECRET_KEY                       read access to payments, balance and payouts
    CRM_LEDGER_URL, CRM_LEDGER_TOKEN        crm-income API and its owner token
    EMPIRE_TRANSACTIONS_API_URL, EMPIRE_RECONCILIATIONS_API_URL, EMPIRE_FINANCE_ACCOUNTS_API_URL
    STRIPE_RECONCILER_TOKEN                 automation ``stripe-reconciler`` (cell writes)
    MONITOR_URL, HEARTBEAT_TOKEN            optional heartbeat ``stripe-reconcile``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from ._sentry import capture

STRIPE_API = "https://api.stripe.com/v1"
BANGKOK = timezone(timedelta(hours=7))
OWNER_SCOPE = "solo_empire"
STRIPE_ACCOUNT = "Stripe balance"
ZERO_DECIMAL = {
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
}
PAYMENT_TYPES = {"charge", "payment"}


def _major(amount: int, currency: str) -> float:
    return float(amount) if currency.lower() in ZERO_DECIMAL else round(amount / 100, 2)


def _day(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), BANGKOK).date().isoformat()


def _notes(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("notes") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class Report:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "fee_recorded": 0,
            "would_record_fee": 0,
            "fee_pending": 0,
            "payout_reconciled": 0,
            "would_reconcile": 0,
            "payout_unchanged": 0,
            "errors": 0,
        }
    )

    def log(self, event: str, **fields: Any) -> None:
        counter = "errors" if event == "error" else event
        if counter in self.counts:
            self.counts[counter] += 1
        print(
            json.dumps(
                {"event": f"stripe_reconcile.{event}", **fields}, ensure_ascii=False
            )
        )


class Stripe:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def each(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        while True:
            page = self.get(
                path,
                limit=100,
                **params,
                **({"starting_after": after} if after else {}),
            )
            out.extend(page["data"])
            if not page.get("has_more") or not page["data"]:
                return out
            after = page["data"][-1]["id"]

    def balance_transaction(
        self, stripe_ids: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The balance transaction of a ledger row's payment, or None while Stripe has none yet."""
        intent = stripe_ids.get("payment_intent")
        invoice = stripe_ids.get("invoice")
        if not intent and invoice:
            found = self.get(
                f"/invoices/{invoice}", **{"expand[]": "charge.balance_transaction"}
            )
            charge = found.get("charge")
            if isinstance(charge, dict) and isinstance(
                charge.get("balance_transaction"), dict
            ):
                return charge["balance_transaction"]
            payments = self.get("/invoice_payments", invoice=invoice).get("data", [])
            paid = [p for p in payments if p.get("status") == "paid"]
            intent = (
                ((paid[0].get("payment") or {}).get("payment_intent")) if paid else None
            )
        if not intent:
            return None
        found = self.get(
            f"/payment_intents/{intent}",
            **{"expand[]": "latest_charge.balance_transaction"},
        )
        charge = found.get("latest_charge")
        bt = charge.get("balance_transaction") if isinstance(charge, dict) else None
        return bt if isinstance(bt, dict) else None


class Ledger:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def stripe_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            response = self.client.get(
                "/api/v1/crm/ledger/revenue", params={"limit": 500, "offset": len(rows)}
            )
            response.raise_for_status()
            page = response.json()["items"]
            rows.extend(page)
            if len(page) < 500:
                return [r for r in rows if _notes(r).get("source") == "stripe-webhook"]

    def set_notes(self, row_id: int, notes: Mapping[str, Any]) -> None:
        response = self.client.patch(
            f"/api/v1/crm/ledger/revenue/{row_id}",
            json={
                "notes": json.dumps(notes, ensure_ascii=False, separators=(",", ":"))
            },
        )
        response.raise_for_status()


class Cell:
    def __init__(self, client: httpx.Client, plural: str) -> None:
        self.client, self.plural = client, plural

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            response = self.client.get(
                f"/v1/{self.plural}",
                params={**filters, "limit": 1000, "offset": len(out)},
            )
            response.raise_for_status()
            page = response.json()["items"]
            out.extend(page)
            if len(page) < 1000:
                return out

    def create(self, body: Mapping[str, Any], key: str) -> dict[str, Any]:
        response = self.client.post(
            f"/v1/{self.plural}", json=body, headers={"Idempotency-Key": key}
        )
        response.raise_for_status()
        return response.json()


@dataclass
class Finance:
    accounts: Cell
    transactions: Cell
    reconciliations: Cell


def record_fees(
    stripe: Stripe,
    ledger: Ledger,
    rows: list[dict[str, Any]],
    report: Report,
    *,
    dry_run: bool,
) -> None:
    for row in rows:
        notes = _notes(row)
        if notes.get("fee") is not None:
            continue
        try:
            bt = stripe.balance_transaction(notes.get("stripe") or {})
            if bt is None:
                report.log("fee_pending", revenue_id=row["id"])
                continue
            currency = str(bt["currency"])
            update = {
                **notes,
                "fee": f"{_major(bt['fee'], currency):.2f}",
                "net": f"{_major(bt['net'], currency):.2f}",
                "settlement_currency": currency.upper(),
                "balance_transaction": bt["id"],
            }
            if dry_run:
                row["notes"] = json.dumps(
                    update
                )  # in memory only, so the payout preview can match it
                report.log(
                    "would_record_fee",
                    revenue_id=row["id"],
                    fee=update["fee"],
                    currency=currency.upper(),
                )
                continue
            ledger.set_notes(int(row["id"]), update)
            row["notes"] = json.dumps(update)
            report.log(
                "fee_recorded",
                revenue_id=row["id"],
                fee=update["fee"],
                currency=currency.upper(),
            )
        except httpx.HTTPError as exc:
            capture(exc)
            report.log(
                "error", step="fee", revenue_id=row["id"], kind=type(exc).__name__
            )


def ensure_account(
    cell: Cell,
    name: str,
    account_type: str,
    institution: str,
    key_id: str,
    *,
    dry_run: bool,
    metadata: Mapping[str, Any] | None = None,
) -> int | None:
    for account in cell.list(account_type=account_type, owner_scope=OWNER_SCOPE):
        if account.get("name") == name:
            return int(account["id"])
    if dry_run:
        return None
    created = cell.create(
        {
            "name": name,
            "account_type": account_type,
            "owner_scope": OWNER_SCOPE,
            "institution": institution,
            "currency": "THB",
            "status": "active",
            "metadata": dict(metadata or {}),
        },
        key=f"stripe:account:{account_type}:{key_id}"[:128],
    )
    return int(created["id"])


def reconcile_payout(
    payout: dict[str, Any],
    stripe: Stripe,
    ledger: Ledger,
    finance: Finance,
    rows: list[dict[str, Any]],
    done: set[str],
    report: Report,
    *,
    dry_run: bool,
) -> None:
    payout_id = payout["id"]
    if payout_id in done:
        report.log("payout_unchanged", payout=payout_id)
        return
    currency = str(payout["currency"])
    txns = [
        t
        for t in stripe.each("/balance_transactions", payout=payout_id)
        if t["type"] != "payout"
    ]
    by_bt = {
        _notes(r).get("balance_transaction"): r
        for r in rows
        if _notes(r).get("balance_transaction")
    }
    payments = [t for t in txns if t["type"] in PAYMENT_TYPES]
    matched = [by_bt[t["id"]] for t in payments if t["id"] in by_bt]
    unmatched = [t["id"] for t in payments if t["id"] not in by_bt]
    expected = _major(sum(t["net"] for t in txns), currency)
    paid = _major(payout["amount"], currency)
    fees = _major(sum(t["fee"] for t in txns), currency)
    difference = round(expected - paid, 2)
    status = "matched" if abs(difference) < 0.005 and not unmatched else "exception"
    destination = (
        payout.get("destination") if isinstance(payout.get("destination"), dict) else {}
    )
    bank = f"{destination.get('bank_name') or 'Bank'} ••{destination.get('last4') or '????'}"
    summary = {
        "payout": payout_id,
        "amount": paid,
        "fees": fees,
        "expected": expected,
        "status": status,
        "matched_revenue_ids": [r["id"] for r in matched],
        "unmatched_balance_transactions": unmatched,
    }
    if dry_run:
        report.log("would_reconcile", **summary)
        return
    stripe_account = ensure_account(
        finance.accounts, STRIPE_ACCOUNT, "payment", "Stripe", "balance", dry_run=False
    )
    bank_account = ensure_account(
        finance.accounts,
        bank,
        "bank",
        str(destination.get("bank_name") or ""),
        str(destination.get("id") or "bank"),
        dry_run=False,
        metadata={"stripe_external_account": destination.get("id")},
    )
    arrival = _day(payout["arrival_date"])
    meta = {"stripe_payout": payout_id, "balance_transactions": [t["id"] for t in txns]}
    transfer = finance.transactions.create(
        {
            "transaction_date": arrival,
            "transaction_type": "transfer",
            "amount": paid,
            "currency": currency.upper(),
            "account_id": bank_account,
            "owner_scope": OWNER_SCOPE,
            "category": "stripe_payout",
            "merchant": "Stripe",
            "description": f"Stripe payout {payout_id}",
            "source": "stripe",
            "metadata": {**meta, "from_account_id": stripe_account},
        },
        key=f"stripe:{payout_id}:transfer",
    )
    fee_txn = None
    if fees > 0:
        fee_txn = finance.transactions.create(
            {
                "transaction_date": arrival,
                "transaction_type": "fee",
                "amount": fees,
                "currency": currency.upper(),
                "account_id": stripe_account,
                "owner_scope": OWNER_SCOPE,
                "category": "payment_processing_fee",
                "merchant": "Stripe",
                "description": f"Stripe fees in payout {payout_id}",
                "source": "stripe",
                "metadata": meta,
            },
            key=f"stripe:{payout_id}:fee",
        )
    for row in matched:
        notes = _notes(row)
        if notes.get("cash_reconciled") and notes.get("payout") == payout_id:
            continue
        ledger.set_notes(
            int(row["id"]), {**notes, "cash_reconciled": True, "payout": payout_id}
        )
    started = min((t["created"] for t in txns), default=payout["created"])
    finance.reconciliations.create(
        {
            "account_id": stripe_account,
            "period_start": _day(started),
            "period_end": arrival,
            "source_type": "wallet_export",
            "source_ref": payout_id,
            "expected_amount": expected,
            "reconciled_amount": paid,
            "difference_amount": difference,
            "currency": currency.upper(),
            "status": status,
            "reconciled_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "notes": json.dumps(
                {
                    **summary,
                    "transfer_transaction_id": transfer.get("id"),
                    "fee_transaction_id": (fee_txn or {}).get("id"),
                },
                separators=(",", ":"),
            ),
        },
        key=f"stripe:{payout_id}:reconciliation",
    )
    report.log("payout_reconciled", **summary)


def run(
    stripe: Stripe,
    ledger: Ledger,
    finance: Finance,
    report: Report,
    *,
    dry_run: bool,
    days: int,
    payout_status: str,
) -> None:
    rows = ledger.stripe_rows()
    record_fees(stripe, ledger, rows, report, dry_run=dry_run)
    done = {
        str(r.get("source_ref"))
        for r in finance.reconciliations.list(source_type="wallet_export")
    }
    since = int(time.time()) - days * 86400
    payouts = stripe.each(
        "/payouts",
        status=payout_status,
        **{"arrival_date[gte]": since, "expand[]": "data.destination"},
    )
    for payout in sorted(payouts, key=lambda p: p["arrival_date"]):
        try:
            reconcile_payout(
                payout, stripe, ledger, finance, rows, done, report, dry_run=dry_run
            )
        except httpx.HTTPError as exc:
            capture(exc)
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            report.log(
                "error",
                step="payout",
                payout=payout["id"],
                kind=type(exc).__name__,
                status=status,
            )


def heartbeat(env: Mapping[str, str], ok: bool, detail: str) -> None:
    url, token = env.get("MONITOR_URL"), env.get("HEARTBEAT_TOKEN")
    if not url or not token:
        return
    try:
        httpx.post(
            f"{url.rstrip('/')}/heartbeat/stripe-reconcile",
            json={"ok": ok, "detail": detail},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        print(json.dumps({"event": "stripe_reconcile.heartbeat", "ok": False}))


def _client(base: str, token: str, **headers: str) -> httpx.Client:
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}", **headers},
        timeout=30,
        follow_redirects=False,
    )  # never forward a token


def _https(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" or parts.hostname in ("127.0.0.1", "localhost")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-stripe-reconcile", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="read only; print what would change"
    )
    parser.add_argument(
        "--days", type=int, default=90, help="payouts that arrived in the last N days"
    )
    parser.add_argument(
        "--payout-status", default="paid", choices=("paid", "in_transit")
    )
    args = parser.parse_args(argv)
    if args.payout_status != "paid" and not args.dry_run:
        parser.error("--payout-status in_transit is a dry-run preview only")
    env = os.environ if env is None else env
    names = (
        "STRIPE_SECRET_KEY",
        "CRM_LEDGER_URL",
        "CRM_LEDGER_TOKEN",
        "EMPIRE_TRANSACTIONS_API_URL",
        "EMPIRE_RECONCILIATIONS_API_URL",
        "EMPIRE_FINANCE_ACCOUNTS_API_URL",
        "STRIPE_RECONCILER_TOKEN",
    )
    config = {name: env.get(name, "").strip() for name in names}
    if missing := [name for name, value in config.items() if not value]:
        print(
            json.dumps(
                {
                    "event": "stripe_reconcile.run",
                    "error": "missing_config",
                    "missing": missing,
                }
            )
        )
        return 2
    if bad := [
        name for name in names if name.endswith("_URL") and not _https(config[name])
    ]:
        print(
            json.dumps(
                {
                    "event": "stripe_reconcile.run",
                    "error": "url_must_be_https",
                    "names": bad,
                }
            )
        )
        return 2
    report = Report()
    cell = {"X-Empire-Channel": "workflow"}
    token = config["STRIPE_RECONCILER_TOKEN"]
    clients = [
        _client(STRIPE_API, config["STRIPE_SECRET_KEY"]),
        _client(config["CRM_LEDGER_URL"], config["CRM_LEDGER_TOKEN"]),
        _client(config["EMPIRE_FINANCE_ACCOUNTS_API_URL"], token, **cell),
        _client(config["EMPIRE_TRANSACTIONS_API_URL"], token, **cell),
        _client(config["EMPIRE_RECONCILIATIONS_API_URL"], token, **cell),
    ]
    try:
        finance = Finance(
            Cell(clients[2], "accounts"),
            Cell(clients[3], "transactions"),
            Cell(clients[4], "reconciliations"),
        )
        run(
            Stripe(clients[0]),
            Ledger(clients[1]),
            finance,
            report,
            dry_run=args.dry_run,
            days=args.days,
            payout_status=args.payout_status,
        )
    except httpx.HTTPError as exc:  # a source that is down fails the run, not silently
        capture(exc)
        status = (
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        )
        report.log("error", step="read", kind=type(exc).__name__, status=status)
    finally:
        for client in clients:
            client.close()
    ok = report.counts["errors"] == 0
    print(
        json.dumps(
            {
                "event": "stripe_reconcile.run",
                "dry_run": args.dry_run,
                "ok": ok,
                **report.counts,
            }
        )
    )
    if not args.dry_run:
        heartbeat(env, ok, f"errors={report.counts['errors']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
