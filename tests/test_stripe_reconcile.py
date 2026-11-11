"""Stripe reconcile: fees onto ledger rows, payouts into transactions + reconciliations, idempotent re-runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from empire_platform import stripe_reconcile as rec

NOW = int(datetime(2026, 10, 27, tzinfo=UTC).timestamp())
PAID_AT = NOW - 9 * 86400
ARRIVAL = NOW - 86400  # 2026-10-26 07:00 Bangkok


class World:
    """In-memory Stripe, crm-income ledger and the three finance cells."""

    def __init__(self) -> None:
        self.revenue = [
            self._row(1, {"stripe": {"payment_intent": "pi_1"}}),
            self._row(2, {"stripe": {"invoice": "in_1", "subscription": "sub_1"}}),
            {
                "id": 3,
                "notes": json.dumps({"recovery_source": "manual"}),
            },  # not from the webhook
        ]
        self.bts = {
            "txn_pi1": {
                "id": "txn_pi1",
                "type": "charge",
                "amount": 690000,
                "fee": 25185,
                "net": 664815,
                "currency": "thb",
                "created": PAID_AT,
            },
            "txn_in1": {
                "id": "txn_in1",
                "type": "payment",
                "amount": 1490000,
                "fee": 24585,
                "net": 1465415,
                "currency": "thb",
                "created": PAID_AT,
            },
            "txn_old": {
                "id": "txn_old",
                "type": "payment",
                "amount": 1000,
                "fee": 18,
                "net": 982,
                "currency": "thb",
                "created": PAID_AT,
            },
        }
        self.payouts = [
            {
                "id": "po_1",
                "status": "paid",
                "amount": 664815 + 1465415,
                "currency": "thb",
                "arrival_date": ARRIVAL,
                "created": ARRIVAL - 3600,
                "destination": {
                    "id": "ba_1",
                    "bank_name": "KASIKORNBANK",
                    "last4": "1234",
                },
            }
        ]
        self.in_payout = {"po_1": ["txn_pi1", "txn_in1"]}
        self.cells: dict[str, list[dict]] = {
            "accounts": [],
            "transactions": [],
            "reconciliations": [],
        }
        self.keys: dict[str, dict] = {}
        self.writes: list[str] = []

    @staticmethod
    def _row(row_id: int, extra: dict) -> dict:
        notes = {
            "source": "stripe-webhook",
            "gross": "0",
            "fee": None,
            "net": None,
            "cash_reconciled": False,
            **extra,
        }
        return {"id": row_id, "notes": json.dumps(notes)}

    def notes(self, row_id: int) -> dict:
        return json.loads(next(r for r in self.revenue if r["id"] == row_id)["notes"])

    def stripe(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path, query = (
            request.url.path.removeprefix("/v1"),
            parse_qs(request.url.query.decode()),
        )
        if path == "/payment_intents/pi_1":
            return httpx.Response(
                200,
                json={"latest_charge": {"balance_transaction": self.bts["txn_pi1"]}},
            )
        if path == "/payment_intents/pi_in1":
            return httpx.Response(
                200,
                json={"latest_charge": {"balance_transaction": self.bts["txn_in1"]}},
            )
        if path == "/invoices/in_1":
            return httpx.Response(200, json={"id": "in_1", "charge": None})
        if path == "/invoice_payments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"status": "paid", "payment": {"payment_intent": "pi_in1"}}
                    ]
                },
            )
        if path == "/payouts":
            assert query["expand[]"] == ["data.destination"]
            found = [p for p in self.payouts if p["status"] == query["status"][0]]
            return httpx.Response(200, json={"data": found, "has_more": False})
        if path == "/balance_transactions":
            assert query["expand[]"] == ["data.source"]
            ids = self.in_payout[query["payout"][0]]
            payout = {
                "id": "txn_po",
                "type": "payout",
                "amount": 0,
                "fee": 0,
                "net": 0,
                "created": ARRIVAL,
            }
            return httpx.Response(
                200,
                json={"data": [self.bts[i] for i in ids] + [payout], "has_more": False},
            )
        return httpx.Response(404, json={"error": {"message": "no"}})

    def ledger(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": self.revenue})
        row_id = int(request.url.path.rsplit("/", 1)[1])
        row = next(r for r in self.revenue if r["id"] == row_id)
        if json.loads(row["notes"]).get("source") == "stripe-webhook-adjustment":
            return httpx.Response(409, json={"code": "adjustment_notes_locked"})
        row["notes"] = json.loads(request.content)["notes"]
        self.writes.append(f"ledger:{row_id}")
        return httpx.Response(200, json={"revenue": row})

    def cell(self, plural: str):
        def handle(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Empire-Channel"] == "workflow"
            items = self.cells[plural]
            if request.method == "GET":
                query = {
                    k: v[0] for k, v in parse_qs(request.url.query.decode()).items()
                }
                found = [
                    i
                    for i in items
                    if all(
                        str(i.get(k)) == v
                        for k, v in query.items()
                        if k not in ("limit", "offset")
                    )
                ]
                return httpx.Response(200, json={"items": found})
            key = request.headers["Idempotency-Key"]
            if key in self.keys:
                return httpx.Response(201, json=self.keys[key])
            record = {"id": len(items) + 1, **json.loads(request.content)}
            items.append(record)
            self.keys[key] = record
            self.writes.append(f"{plural}:{key}")
            return httpx.Response(201, json=record)

        return handle

    def run(self, *, dry_run: bool = False, status: str = "paid") -> rec.Report:
        def client(handler) -> httpx.Client:
            return httpx.Client(
                base_url="https://x.test",
                transport=httpx.MockTransport(handler),
                headers={"X-Empire-Channel": "workflow"},
            )

        finance = rec.Finance(
            rec.Cell(client(self.cell("accounts")), "accounts"),
            rec.Cell(client(self.cell("transactions")), "transactions"),
            rec.Cell(client(self.cell("reconciliations")), "reconciliations"),
        )
        report = rec.Report()
        rec.run(
            rec.Stripe(client(self.stripe)),
            rec.Ledger(client(self.ledger)),
            finance,
            report,
            dry_run=dry_run,
            days=90,
            payout_status=status,
        )
        return report


def test_fees_and_payout_are_reconciled_once():
    world = World()
    report = world.run()
    assert report.counts["fee_recorded"] == 2
    assert report.counts["payout_reconciled"] == 1
    assert report.counts["errors"] == 0
    first = world.notes(1)
    assert (first["fee"], first["net"], first["settlement_currency"]) == (
        "251.85",
        "6648.15",
        "THB",
    )
    assert first["balance_transaction"] == "txn_pi1"
    assert first["cash_reconciled"] is True
    assert first["payout"] == "po_1"
    assert world.notes(2)["balance_transaction"] == "txn_in1"
    assert world.notes(2)["cash_reconciled"] is True
    assert "fee" not in json.loads(
        world.revenue[2]["notes"]
    )  # rows not from the webhook are left alone

    accounts = {a["account_type"]: a for a in world.cells["accounts"]}
    assert accounts["payment"]["name"] == "Stripe balance"
    assert accounts["bank"]["name"] == "KASIKORNBANK ••1234"
    assert all(a["scope"] == "solo_empire" for a in accounts.values())
    assert not any("owner_scope" in a for a in accounts.values())
    transfer, fee = world.cells["transactions"]
    assert (
        transfer["transaction_type"],
        transfer["amount"],
        transfer["transaction_date"],
    ) == ("transfer", 21302.30, "2026-10-26")
    assert transfer["account_id"] == accounts["bank"]["id"]
    assert (fee["transaction_type"], fee["amount"], fee["account_id"]) == (
        "fee",
        497.70,
        accounts["payment"]["id"],
    )
    [recon] = world.cells["reconciliations"]
    assert (recon["source_ref"], recon["status"], recon["difference_amount"]) == (
        "po_1",
        "matched",
        0,
    )
    assert (recon["expected_amount"], recon["reconciled_amount"]) == (
        21302.30,
        21302.30,
    )
    assert json.loads(recon["notes"])["matched_revenue_ids"] == [1, 2]

    writes = len(world.writes)
    again = world.run()
    assert again.counts["payout_unchanged"] == 1
    assert again.counts["fee_recorded"] == 0
    assert len(world.writes) == writes  # a re-run writes nothing


def test_unmatched_payment_makes_an_exception():
    world = World()
    world.in_payout["po_1"].append(
        "txn_old"
    )  # a payment with no ledger row (before the webhook)
    world.payouts[0]["amount"] += 982
    world.run()
    [recon] = world.cells["reconciliations"]
    assert recon["status"] == "exception"
    assert json.loads(recon["notes"])["unmatched_balance_transactions"] == ["txn_old"]


def test_dry_run_writes_nothing_and_previews_in_transit():
    world = World()
    world.payouts[0]["status"] = "in_transit"
    report = world.run(dry_run=True, status="in_transit")
    assert report.counts["would_record_fee"] == 2
    assert report.counts["would_reconcile"] == 1
    assert world.writes == []
    assert world.notes(1)["fee"] is None


def test_missing_balance_transaction_waits():
    world = World()
    world.revenue = [World._row(9, {"stripe": {"payment_intent": "pi_unknown"}})]
    world.payouts = []
    report = world.run()
    assert report.counts["errors"] == 1  # Stripe 404 is reported, the run continues
    assert world.notes(9)["fee"] is None


def test_main_needs_config_and_https(capsys):
    assert rec.main([], env={}) == 2
    assert "missing_config" in capsys.readouterr().out
    env = dict.fromkeys(
        ("STRIPE_SECRET_KEY", "CRM_LEDGER_TOKEN", "STRIPE_RECONCILER_TOKEN"), "x"
    )
    env |= dict.fromkeys(
        (
            "CRM_LEDGER_URL",
            "EMPIRE_TRANSACTIONS_API_URL",
            "EMPIRE_RECONCILIATIONS_API_URL",
            "EMPIRE_FINANCE_ACCOUNTS_API_URL",
        ),
        "http://example.com",
    )
    assert rec.main(["--dry-run"], env=env) == 2
    assert "url_must_be_https" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        rec.main(["--payout-status", "in_transit"], env=env)


ADJ = "stripe-webhook-adjustment"


def _adjustment(row_id: int, entry: str, amount: float, stripe: dict) -> dict:
    notes = {"source": ADJ, "entry": entry, "stripe": stripe}
    return {
        "id": row_id,
        "entry_type": entry,
        "amount": amount,
        "notes": json.dumps(notes),
    }


def _refund_bt(
    txn: str, refund: str, charge: str, amount: int, kind: str = "refund"
) -> dict:
    return {
        "id": txn,
        "type": kind,
        "amount": -amount,
        "fee": 0,
        "net": -amount,
        "currency": "thb",
        "created": PAID_AT + 3600,
        "source": {"id": refund, "object": "refund", "charge": charge},
    }


def _dispute_bt(txn: str, amount: int, fee: int, category: str = "dispute") -> dict:
    return {
        "id": txn,
        "type": "adjustment",
        "reporting_category": category,
        "amount": amount,
        "fee": fee,
        "net": amount - fee,
        "currency": "thb",
        "created": PAID_AT + 7200,
        "source": {"id": "dp_1", "object": "dispute", "charge": "ch_1"},
    }


def _add(world: World, *bts: dict) -> None:
    for bt in bts:
        world.bts[bt["id"]] = bt
        world.in_payout["po_1"].append(bt["id"])
        world.payouts[0]["amount"] += bt["net"]


def test_adjustment_rows_are_not_payments():
    world = World()
    world.revenue.append(
        _adjustment(10, "refund", -1000.0, {"charge": "ch_1", "refund": "re_1"})
    )
    report = world.run()
    assert report.counts["errors"] == 0
    assert report.counts["fee_recorded"] == 2  # only the two payments
    assert "fee" not in world.notes(10)
    assert "ledger:10" not in world.writes  # adjustment notes are locked upstream


def test_refund_matches_its_adjustment_row():
    world = World()
    world.revenue.append(
        _adjustment(10, "refund", -1000.0, {"charge": "ch_1", "refund": "re_1"})
    )
    _add(world, _refund_bt("txn_re1", "re_1", "ch_1", 100000))
    world.run()
    [recon] = world.cells["reconciliations"]
    notes = json.loads(recon["notes"])
    assert recon["status"] == "matched"
    assert notes["matched_adjustment_ids"] == [10]
    assert notes["unmatched_balance_transactions"] == []
    assert recon["expected_amount"] == recon["reconciled_amount"] == 20302.30


def test_refund_matches_the_fallback_row_by_charge():
    world = World()
    world.revenue.append(_adjustment(10, "refund", -1000.0, {"charge": "ch_1"}))
    _add(world, _refund_bt("txn_re1", "re_1", "ch_1", 100000, kind="payment_refund"))
    world.run()
    [recon] = world.cells["reconciliations"]
    assert recon["status"] == "matched"
    assert json.loads(recon["notes"])["matched_adjustment_ids"] == [10]


def test_refund_without_ledger_row_is_an_exception():
    world = World()
    _add(world, _refund_bt("txn_re1", "re_1", "ch_1", 100000))
    _add(world, _refund_bt("txn_rf1", "re_2", "ch_1", -50000, kind="refund_failure"))
    world.run()
    [recon] = world.cells["reconciliations"]
    assert recon["status"] == "exception"
    assert json.loads(recon["notes"])["unmatched_balance_transactions"] == [
        "txn_re1",
        "txn_rf1",
    ]


def test_dispute_matches_and_its_fee_is_not_counted_twice():
    world = World()
    world.revenue += [
        _adjustment(
            11,
            "dispute",
            -6900.0,
            {"dispute": "dp_1", "balance_transaction": "txn_dp1"},
        ),
        _adjustment(
            12,
            "dispute_fee",
            -15.0,
            {"dispute": "dp_1", "balance_transaction": "txn_dp1"},
        ),
    ]
    _add(world, _dispute_bt("txn_dp1", -690000, 1500))
    world.run()
    [recon] = world.cells["reconciliations"]
    notes = json.loads(recon["notes"])
    assert recon["status"] == "matched"
    assert notes["matched_adjustment_ids"] == [11, 12]
    assert notes["dispute_fees_in_ledger"] == 15.0
    assert notes["fees"] == 497.70
    _, fee = world.cells["transactions"]
    assert (
        fee["amount"] == 497.70
    )  # payment fees only; the dispute fee is the ledger's dispute_fee row
    assert (
        recon["expected_amount"]
        == recon["reconciled_amount"]
        == round(21302.30 - 6915.0, 2)
    )


def test_dispute_reversal_matches_and_unrecorded_fee_stays_in_fee_transaction():
    world = World()
    world.revenue.append(
        _adjustment(
            13, "dispute", 6900.0, {"dispute": "dp_1", "balance_transaction": "txn_dp2"}
        )
    )
    _add(world, _dispute_bt("txn_dp2", 690000, 0, category="dispute_reversal"))
    _add(world, _dispute_bt("txn_dp3", -690000, 1500))  # no ledger rows at all
    world.run()
    [recon] = world.cells["reconciliations"]
    notes = json.loads(recon["notes"])
    assert recon["status"] == "exception"
    assert notes["matched_adjustment_ids"] == [13]
    assert notes["unmatched_balance_transactions"] == ["txn_dp3"]
    assert notes["dispute_fees_in_ledger"] == 0
    _, fee = world.cells["transactions"]
    assert fee["amount"] == round(497.70 + 15.0, 2)


def test_dispute_preview_and_rerun():
    world = World()
    world.revenue += [
        _adjustment(11, "dispute", -6900.0, {"balance_transaction": "txn_dp1"}),
        _adjustment(12, "dispute_fee", -15.0, {"balance_transaction": "txn_dp1"}),
    ]
    _add(world, _dispute_bt("txn_dp1", -690000, 1500))
    world.run(dry_run=True)
    assert world.writes == []
    world.run()
    writes = len(world.writes)
    assert world.run().counts["payout_unchanged"] == 1
    assert len(world.writes) == writes
