"""The month-end close pack: the control roll foots, the pack is a
pure read over the books, and regeneration is byte-identical."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.close_pack import (
    build_close_pack,
    close_pack_export,
    pack_digest,
    render_close_pack_text,
)
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.models import SuspenseItem
from cash_recon.objects import InMemoryObjectStore
from cash_recon.report import broker_scorecard
from cash_recon.store import InMemoryStore
from tests.conftest import run_through

D = Decimal


@pytest.fixture()
def client():
    service = Service(InMemoryStore(), InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    return TestClient(create_app(service))


def _september(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 22))
    return store


def test_the_roll_foots_and_agrees_with_the_ledger(world):
    store = _september(world)
    body = build_close_pack(store, 2026, 9)
    roll = body["control_roll"]
    assert roll["opening_suspense"] == "3280.00"  # the two seed items
    assert roll["footed"] is True and roll["difference"] == "0.00"
    opening = D(roll["opening_suspense"])
    received = D(roll["received_into_suspense"])
    applied = D(roll["applied_from_suspense"])
    assert opening + received - applied == D(roll["closing_suspense"])
    # The ledger agrees: every still-open item's money is in the close.
    ledger = sum(
        (i.amount for i in store.suspense_items() if i.status == "open"), D("0.00")
    )
    assert D(roll["closing_suspense"]) == ledger


def test_the_pack_lists_the_months_packs_with_approvers(world):
    store = _september(world)
    body = build_close_pack(store, 2026, 9)
    packs = {p["pack_id"]: p for p in body["packs_approved_in_month"]}
    assert "PACK-2026-09-02" in packs
    assert packs["PACK-2026-09-02"]["approvals"] == ["Priya Nair"]
    assert packs["PACK-2026-09-02"]["allocation_total"] == "18031.60"
    assert body["control_roll"]["approved_write_offs"] == "0.00"


def test_regeneration_is_byte_identical(world):
    store = _september(world)
    first = close_pack_export(store, 2026, 9)
    second = close_pack_export(store, 2026, 9)
    assert first == second
    assert render_close_pack_text(first) == render_close_pack_text(second)
    assert first["sha256"] == pack_digest(first["pack"])
    # The body carries no wall-clock: decided_at lives on pack rows,
    # and the digest is over the body alone.
    assert "generated_as_of" not in first["pack"]


def test_an_untraceable_item_makes_the_pack_say_not_footed(world):
    store = _september(world)
    store.add_suspense(SuspenseItem(
        suspense_id="SUS-GHOST", bank_line_id="BL-GHOST", amount=D("777.00"),
        received_date=date(2026, 9, 10), reason="UNIDENTIFIED"))
    body = build_close_pack(store, 2026, 9)
    roll = body["control_roll"]
    assert roll["footed"] is False
    assert "NOT FOOTED" in roll["footing_note"]
    assert body["discrepancies"] and body["discrepancies"][0]["amount"] == "777.00"
    text = render_close_pack_text(close_pack_export(store, 2026, 9))
    assert "NOT FOOTED" in text and "BL-GHOST" in text


def test_assembling_the_pack_mutates_nothing(world):
    store = _september(world)
    before = (
        len(store.suspense_items()), len(store.packs()),
        len(store.exceptions()), len(store.allocations()),
    )
    build_close_pack(store, 2026, 9)
    after = (
        len(store.suspense_items()), len(store.packs()),
        len(store.exceptions()), len(store.allocations()),
    )
    assert before == after


def test_month_scorecard_is_scoped_to_the_month(world):
    store = _september(world)
    card = {r["broker_id"]: r for r in broker_scorecard(store, date(2026, 9, 30), month=(2026, 9))}
    # Alder's September: the 41,252.22 wire and nothing else; the two
    # opening suspense items belong to no broker and no other month
    # leaks in.
    assert card["BRK-ALDER"]["bank_total"] == "41252.22"
    assert card["BRK-ALDER"]["remittances_received"] == 2
    cumulative = {r["broker_id"]: r for r in broker_scorecard(store, date(2026, 9, 30))}
    assert cumulative["BRK-ALDER"]["bank_total"] == "41252.22"  # same here: all Alder cash is September's


def test_close_pack_over_http(client):
    client.post("/api/seed")
    client.post("/api/days/2026-09-02/run", json={"actor": "cash-team"})
    client.post("/api/packs/PACK-2026-09-02/approve", json={"approver": "Priya Nair"})
    got = client.get("/api/close/2026-09")
    assert got.status_code == 200
    body = got.json()
    assert body["month"] == "2026-09" and len(body["sha256"]) == 64
    assert body["pack"]["control_roll"]["footed"] is True
    assert body["pack"]["packs_approved_in_month"][0]["pack_id"] == "PACK-2026-09-02"
    text = client.get("/api/close/2026-09/text")
    assert text.status_code == 200 and "MONTH-END CLOSE PACK — 2026-09" in text.text
    assert client.get("/api/close/2026-13").status_code == 400


def test_september_close_carries_a_zero_payable_section(world):
    store = _september(world)
    export = close_pack_export(store, 2026, 9)
    rp = export["pack"]["return_premiums"]
    assert rp["opening_payables"] == "0.00" and rp["closing_payables"] == "0.00"
    assert rp["footed"] is True and rp["discrepancies"] == []
    # No payable activity: the text rendering stays as it was.
    assert "RETURN PREMIUMS" not in render_close_pack_text(export)


def test_the_payable_ledger_rolls_on_its_own_identity():
    from cash_recon.models import PayableEvent, ReturnPremiumPayable
    from cash_recon.money import ZERO as _ZERO
    from cash_recon.store import InMemoryStore

    store = InMemoryStore()

    def payable(pid, policy, opened_on, amount, moves):
        events = [PayableEvent(on=opened_on, kind="opened", amount=amount, ref="CXL")]
        events += [PayableEvent(on=d, kind=k, amount=a, ref="X") for d, k, a in moves]
        return ReturnPremiumPayable(
            payable_id=pid, policy_number=policy, cancellation_id="CXL",
            payee_kind="insured", payee_name="Insured", broker_id=None, amount=amount,
            amount_netted=sum((a for _d, k, a in moves if k == "netted"), _ZERO),
            amount_paid=sum((a for _d, k, a in moves if k == "paid"), _ZERO),
            status="paid", opened_on=opened_on, events=events,
        )

    store.save_payable(payable("PAY-A", "NSP-A", date(2026, 11, 1), Decimal("5966.85"),
                               [(date(2026, 12, 5), "paid", Decimal("5966.85"))]))
    store.save_payable(payable("PAY-B", "NSP-B", date(2026, 12, 1), Decimal("3008.22"),
                               [(date(2026, 12, 3), "netted", Decimal("3000.00")),
                                (date(2026, 12, 4), "paid", Decimal("8.22"))]))
    december = close_pack_export(store, 2026, 12)["pack"]
    rp = december["return_premiums"]
    assert rp["opening_payables"] == "5966.85"      # November's payable, still standing
    assert rp["payables_opened_in_month"] == "3008.22"
    assert rp["netted_in_month"] == "3000.00"
    assert rp["paid_in_month"] == "5975.07"
    assert rp["closing_payables"] == "0.00"
    assert rp["footed"] is True and rp["open_payables_at_close"] == []
    # The suspense roll is untouched by any of it.
    assert december["control_roll"]["closing_suspense"] == "0.00"
    november = close_pack_export(store, 2026, 11)["pack"]["return_premiums"]
    assert november["opening_payables"] == "0.00"
    assert november["closing_payables"] == "5966.85"
    text = render_close_pack_text(close_pack_export(store, 2026, 12))
    assert "RETURN PREMIUMS (payable ledger" in text
    assert "The payable ledger FOOTS" in text
