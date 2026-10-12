"""The suspense re-match pass: cash whose explanation arrived late.

Three rescues/stays, all built as small worlds so the arithmetic is
visible: a corrected remittance rescues a wire its first advice
failed; an advice whose own numbers still differ leaves the cash in
suspense under the now-correct code; a policy booked after its wire
landed rescues a reference-led payment. Plus the scope discipline:
a partially-applied line is never re-worked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import approve_pack
from cash_recon.llm import OfflineReader
from cash_recon.models import (
    BankLine,
    Broker,
    Installment,
    PolicyStatus,
    Receivable,
    Remittance,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.rematch import rematch_suspense
from cash_recon.report import day_report
from cash_recon.store import InMemoryStore

D = Decimal
BROKER = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))


def make_policy(number, premium, broker_id="BRK-TEST"):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


def advice(day, lines, total):
    body = [
        "Remittance advice — Testline Brokers",
        "Broker account: BRK-TEST",
        f"Date: {day}",
        "",
        "Premium received for the following policies (net of commission):",
        *[f"Policy {p} — {name} — {amt:,.2f}" for p, name, amt in lines],
        "",
        f"Total remitted: {total:,.2f}",
    ]
    return "\n".join(body)


def world():
    store = InMemoryStore()
    store.upsert_broker(BROKER)
    return store, InMemoryObjectStore()


def run(store, objects, day):
    return run_day(store, objects, OfflineReader(), PolicyConfig(), day, actor="cash-team")


def failed_wire_world():
    """A wire whose advice does not foot, then the broker's
    corrected advice three days later. Returns (store, objects)."""
    store, objects = world()
    store.upsert_receivable(make_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(make_policy("NSP-2002", D("3000.00")))
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-01", broker_id="BRK-TEST", received_date=date(2026, 9, 5),
        stated_total=None,
        text=advice("2026-09-05", [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4500.00")),
    ))
    store.add_bank_line(BankLine(bank_line_id="BL-T1", value_date=date(2026, 9, 5),
                                 amount=D("4400.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    return store, objects


def test_late_corrected_remittance_rescues_the_wire():
    store, objects = failed_wire_world()
    run(store, objects, date(2026, 9, 5))
    item = store.suspense_items()[0]
    assert item.reason == "REMITTANCE_ARITHMETIC" and item.amount == D("4400.00")

    # The broker's corrected advice lands three days later.
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-02", broker_id="BRK-TEST", received_date=date(2026, 9, 8),
        stated_total=None,
        text=advice("2026-09-08", [("NSP-2001", "Alpha Test Co", D("1700.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4400.00")),
    ))
    summary = rematch_suspense(store, OfflineReader(), PolicyConfig(),
                               "cash-team", date(2026, 9, 8))

    assert summary["documents_prepared"] == 1
    assert summary["attempted"] == 1
    assert summary["recovered"] == [{
        "suspense_id": "SUS-BL-T1", "bank_line_id": "BL-T1",
        "amount": "4400.00", "tier": "T4_REMITTANCE",
    }]
    assert summary["pack_id"] == "PACK-REMATCH-2026-09-08"

    item = store.suspense_items()[0]
    assert item.status == "resolved" and item.resolved_on == date(2026, 9, 8)
    # The failed reading is closed; the corrected advice's shortfall
    # on NSP-2001 is on the record instead.
    codes = {(e.code.value, e.status) for e in store.exceptions()}
    assert ("REMITTANCE_ARITHMETIC", "resolved") in codes
    assert ("PARTIAL", "open") in codes

    # The attempt is on the item's audit trail, with the tiers run.
    trail = [e for e in store.audit_events("SUS-BL-T1") if e.action == "suspense_rematch_attempt"]
    assert trail and "T1,T2,T3,T4,T5" in trail[0].detail and "T4_REMITTANCE" in trail[0].detail

    # The recovery stops at the gate like any fresh match — and once
    # approved, the receivables move.
    pack = store.pack(summary["pack_id"])
    assert pack.status.value == "proposed"
    assert store.receivable("NSP-2002").status == PolicyStatus.ACTIVE
    approve_pack(store, summary["pack_id"], "Priya Nair", PolicyConfig())
    assert store.receivable("NSP-2002").status == PolicyStatus.PAID_IN_FULL
    assert store.receivable("NSP-2001").installments[0].amount_applied == D("1888.89")

    # Both days' reports foot, and the recovery day names the line.
    assert day_report(store, date(2026, 9, 5), PolicyConfig())["footing_ok"] is True
    rep8 = day_report(store, date(2026, 9, 8), PolicyConfig())
    assert rep8["rematch_recoveries"]["recovered_total"] == "4400.00"
    assert rep8["rematch_recoveries"]["items"] == [{"bank_line_id": "BL-T1", "amount": "4400.00"}]


def test_advice_that_still_differs_stays_open_with_the_right_code():
    store, objects = world()
    store.upsert_receivable(make_policy("NSP-2009", D("1500.00")))  # net due 1350.00
    store.add_bank_line(BankLine(bank_line_id="BL-T9", value_date=date(2026, 9, 5),
                                 amount=D("1000.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire"))
    run(store, objects, date(2026, 9, 5))
    item = store.suspense_items()[0]
    assert item.reason == "UNIDENTIFIED"

    # The advice arrives, claims the wire's total, but its own lines
    # sum to 950.00 — the cash stays, now under the correct code.
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-09", broker_id="BRK-TEST", received_date=date(2026, 9, 7),
        stated_total=None,
        text=advice("2026-09-07", [("NSP-2009", "Iota Test Co", D("950.00"))], D("1000.00")),
    ))
    summary = rematch_suspense(store, OfflineReader(), PolicyConfig(),
                               "cash-team", date(2026, 9, 7))

    assert summary["recovered"] == [] and summary["pack_id"] is None
    assert summary["still_open"] == [{
        "suspense_id": "SUS-BL-T9", "bank_line_id": "BL-T9",
        "amount": "1000.00", "reason": "REMITTANCE_ARITHMETIC",
    }]
    item = store.suspense_items()[0]
    assert item.status == "open" and item.amount == D("1000.00")
    assert item.reason == "REMITTANCE_ARITHMETIC"
    codes = {e.code.value for e in store.exceptions(status="open")}
    assert "REMITTANCE_ARITHMETIC" in codes


def test_policy_booked_after_the_wire_rescues_it():
    store, objects = world()
    store.add_bank_line(BankLine(bank_line_id="BL-T7", value_date=date(2026, 9, 5),
                                 amount=D("2000.00"), payer_name="Gamma Test Co",
                                 reference="NSP-2010", narrative="premium NSP-2010"))
    run(store, objects, date(2026, 9, 5))
    assert store.suspense_items()[0].reason == "UNKNOWN_POLICY"

    # The policy is booked two days after its payment landed.
    store.upsert_receivable(make_policy("NSP-2010", D("2000.00"), broker_id=None))
    summary = rematch_suspense(store, OfflineReader(), PolicyConfig(),
                               "cash-team", date(2026, 9, 7))

    assert summary["recovered"][0]["tier"] == "T1_EXACT"
    assert store.suspense_items()[0].status == "resolved"
    exc = next(e for e in store.exceptions() if e.code.value == "UNKNOWN_POLICY")
    assert exc.status == "resolved"
    approve_pack(store, summary["pack_id"], "Priya Nair", PolicyConfig())
    assert store.receivable("NSP-2010").status == PolicyStatus.PAID_IN_FULL


def test_partially_applied_lines_are_out_of_scope():
    store, objects = world()
    store.upsert_receivable(make_policy("NSP-2011", D("1000.00"), broker_id=None))
    store.add_bank_line(BankLine(bank_line_id="BL-T11", value_date=date(2026, 9, 5),
                                 amount=D("1400.00"), payer_name="Insured NSP-2011",
                                 reference="NSP-2011", narrative="premium"))
    run(store, objects, date(2026, 9, 5))
    item = store.suspense_items()[0]
    assert item.reason == "OVERPAID" and item.amount == D("400.00")

    summary = rematch_suspense(store, OfflineReader(), PolicyConfig(),
                               "cash-team", date(2026, 9, 9))
    assert summary["attempted"] == 0 and summary["pack_id"] is None
    item = store.suspense_items()[0]
    assert item.status == "open" and item.amount == D("400.00")


def test_rematch_endpoint_runs_the_pass():
    store, objects = failed_wire_world()
    run(store, objects, date(2026, 9, 5))
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-02", broker_id="BRK-TEST", received_date=date(2026, 9, 8),
        stated_total=None,
        text=advice("2026-09-08", [("NSP-2001", "Alpha Test Co", D("1700.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4400.00")),
    ))
    service = Service(store, objects, OfflineReader(), PolicyConfig())
    client = TestClient(create_app(service))
    body = client.post("/api/suspense/rematch",
                       json={"actor": "cash-team", "as_of": "2026-09-08"}).json()
    assert body["recovered"][0]["bank_line_id"] == "BL-T1"
    assert body["pack_id"] == "PACK-REMATCH-2026-09-08"
    ready = client.get("/readiness").json()
    assert ready["profile"] == "memory"
