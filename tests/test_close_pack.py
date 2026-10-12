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
