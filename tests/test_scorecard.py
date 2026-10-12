"""The broker netting scorecard: a read model over the books.

Pinned two ways: the figures it must produce for the brokers the
synthetic month exercises hardest (Copperline's wrong-rate
deduction, Fernway's short wire, Alder's clean lump sum), and the
identity that keeps it honest — its money columns are the daily
reports' by-broker split, summed over the month.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.report import broker_scorecard, day_report
from tests.conftest import run_through

D = Decimal
AS_OF = date(2026, 9, 21)


def _card(store):
    return {r["broker_id"]: r for r in broker_scorecard(store, AS_OF)}


def test_scorecard_figures_for_the_planted_brokers(world):
    _ds, store, objects = world
    run_through(store, objects, AS_OF)
    card = _card(store)
    assert len(card) == 8

    alder = card["BRK-ALDER"]
    assert alder["remittances_received"] == 2  # the advice, and its re-send
    assert alder["allocated_total"] == alder["bank_total"]
    assert alder["suspense_total"] == "0.00"
    assert alder["clean_netting_rate"] == "1.0000"
    assert alder["settled_netting_lines"] == 1 and alder["clean_netting_lines"] == 1
    assert alder["netting_mismatches"] == 0 and alder["avg_days_unapplied"] is None

    copper = card["BRK-COPPER"]
    assert copper["netting_mismatches"] == 1
    assert copper["netting_mismatch_delta"] == "189.60"
    assert copper["clean_netting_rate"] is None  # nothing settled at a netting tier
    # BL-004's wrong-rate wire (5,182.40, 18 days) plus the cancelled
    # Copperline policy's cash on BL-013 (7,280.00, 12 days) — the
    # day reports attribute that line to the broker of record too.
    assert copper["suspense_total"] == "12462.40"
    assert copper["avg_days_unapplied"] == "15.00"
    assert copper["open_exceptions_by_code"] == {
        "CANCELLED_POLICY": {"count": 1, "amount": "7280.00"},
        "NETTING_MISMATCH": {"count": 1, "amount": "189.60"},
        "PARTIAL": {"count": 1, "amount": "75.75"},
    }
    assert len(copper["query_notes"]) == 1
    assert "NSP-1004" in copper["query_notes"][0]["note"]

    fern = card["BRK-FERN"]
    assert fern["clean_netting_rate"] == "1.0000"  # BL-021 settled clean at T2
    assert fern["suspense_total"] == "36974.59"  # the short wire, still unapplied
    assert fern["avg_days_unapplied"] == "13.00"
    assert fern["open_exceptions_by_code"] == {
        "REMITTANCE_ARITHMETIC": {"count": 1, "amount": "100.00"}
    }

    harbor = card["BRK-HARBOR"]
    # The lump sum settled by proposal (tier None), not a netting tier.
    assert harbor["settled_netting_lines"] == 0 and harbor["clean_netting_rate"] is None
    assert harbor["allocated_total"] == harbor["bank_total"]


def test_scorecard_money_foots_to_the_daily_reports(world):
    _ds, store, objects = world
    run_through(store, objects, AS_OF)
    policy = PolicyConfig()
    days = sorted(
        {l.value_date for l in store.bank_lines()} | {r.received_date for r in store.remittances()}
    )
    totals: dict[str, dict[str, Decimal]] = {}
    for day in days:
        for broker_id, split in day_report(store, day, policy)["by_broker"].items():
            slot = totals.setdefault(broker_id, {"bank": D("0"), "allocated": D("0"), "suspense": D("0")})
            for key in slot:
                slot[key] += D(split[key])
    for row in broker_scorecard(store, AS_OF):
        summed = totals[row["broker_id"]]
        assert D(row["bank_total"]) == summed["bank"]
        assert D(row["allocated_total"]) == summed["allocated"]
        assert D(row["suspense_total"]) == summed["suspense"]


def test_scorecard_endpoint(world):
    _ds, store, objects = world
    run_through(store, objects, AS_OF)
    client = TestClient(create_app(Service(store, objects, OfflineReader(), PolicyConfig())))
    rows = client.get("/api/brokers/scorecard?as_of=2026-09-21").json()
    assert len(rows) == 8
    copper = next(r for r in rows if r["broker_id"] == "BRK-COPPER")
    assert copper["netting_mismatches"] == 1 and copper["netting_mismatch_delta"] == "189.60"
