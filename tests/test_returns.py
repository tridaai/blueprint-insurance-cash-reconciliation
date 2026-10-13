"""Returned payments: applied cash the bank took back.

The chain is the point — application, return, re-application are
three records, never edits of one another. These tests pin the
reversal's exactness (prior balances and statuses, including the
broker gross-up), the pack flag that leaves pack bodies untouched,
the suspense the return releases, the reports' returns control
lines, and the re-send that must not die as a duplicate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon import gate
from cash_recon.close_pack import close_pack_export, render_close_pack_text
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.models import (
    BankLine,
    Broker,
    Installment,
    PolicyStatus,
    Receivable,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.report import day_report, suspense_aging
from cash_recon.returns import ReturnError, line_history, record_return
from cash_recon.store import InMemoryStore

D = Decimal
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def _policy(number, premium, broker_id=None, insured=None):
    return Receivable(
        policy_number=number, insured_name=insured or f"Insured {number}",
        broker_id=broker_id, status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


def _world(*receivables, brokers=()):
    store = InMemoryStore()
    for b in brokers:
        store.upsert_broker(b)
    for r in receivables:
        store.upsert_receivable(r)
    return store, InMemoryObjectStore()


def _run_and_approve(store, objects, day):
    result = run_day(store, objects, OfflineReader(), PolicyConfig(), day, actor=ASSEMBLER)
    if result["pack_id"]:
        gate.approve_pack(store, result["pack_id"], CONTROLLER, PolicyConfig())
    return result


def _line(line_id, day, amount, payer, reference=None):
    return BankLine(bank_line_id=line_id, value_date=day, amount=amount,
                    payer_name=payer, reference=reference, narrative="")


def test_t1_return_reopens_items_and_flags_the_pack_without_touching_it():
    store, objects = _world(_policy("NSP-3001", D("1000.00"), insured="Alpha Return Co"))
    store.add_bank_line(_line("BL-R1", date(2026, 9, 3), D("1000.00"), "Alpha Return Co", "NSP-3001"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    rec = store.receivable("NSP-3001")
    assert rec.status == PolicyStatus.PAID_IN_FULL

    pack_before = store.pack("PACK-2026-09-03")
    entries_before = [e.model_dump() for e in pack_before.entries]
    posting_before = gate.posting_file(pack_before)

    ret = record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5))
    assert ret.amount == D("1000.00") and ret.pack_ids == ["PACK-2026-09-03"]

    rec = store.receivable("NSP-3001")
    inst = rec.installments[0]
    assert inst.amount_applied == D("0.00") and inst.status.value == "open"
    assert rec.status == PolicyStatus.ACTIVE and rec.open_balance == D("1000.00")

    pack_after = store.pack("PACK-2026-09-03")
    assert pack_after.flags == ["RETURN_AFFECTED"]
    assert [e.model_dump() for e in pack_after.entries] == entries_before  # body untouched
    assert gate.posting_file(pack_after) == posting_before  # the posted history stands

    report_back = day_report(store, date(2026, 9, 5), PolicyConfig())
    assert report_back["returns_recorded"]["count"] == 1
    assert report_back["returns_recorded"]["total"] == "1000.00"
    origin = day_report(store, date(2026, 9, 3), PolicyConfig())
    assert origin["allocated_total"] == "1000.00"
    assert origin["returned_total"] == "1000.00"
    assert origin["allocated_net_of_returns"] == "0.00"

    history = line_history(store, "BL-R1")
    assert history["return"]["reason"] == "NSF"
    actions = [e["action"] for e in history["events"]]
    assert "pack_approved" in actions and "payment_returned" in actions
    assert history["packs"][0]["flags"] == ["RETURN_AFFECTED"]


def test_return_refusals_state_their_reasons():
    store, objects = _world(_policy("NSP-3001", D("1000.00"), insured="Alpha Return Co"))
    store.add_bank_line(_line("BL-R1", date(2026, 9, 3), D("1000.00"), "Alpha Return Co", "NSP-3001"))
    store.add_bank_line(_line("BL-RX", date(2026, 9, 3), D("250.00"), "Nobody Known", None))
    result = run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 3), actor=ASSEMBLER)

    # The pack is only proposed: nothing is applied yet.
    with pytest.raises(ReturnError, match="not applied"):
        record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 4))
    gate.approve_pack(store, result["pack_id"], CONTROLLER, PolicyConfig())

    # A line that only ever reached suspense is not applied either.
    with pytest.raises(ReturnError, match="not applied"):
        record_return(store, "BL-RX", "NSF", CONTROLLER, date(2026, 9, 4))
    with pytest.raises(ReturnError, match="no such bank line"):
        record_return(store, "BL-NOPE", "NSF", CONTROLLER, date(2026, 9, 4))
    with pytest.raises(ReturnError, match="unknown return reason"):
        record_return(store, "BL-R1", "BOUNCED", CONTROLLER, date(2026, 9, 4))

    # Part of a line coming back is refused, in words, and changes nothing.
    with pytest.raises(ReturnError, match="partial returns are not recorded"):
        record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 4), amount=D("400.00"))
    assert store.receivable("NSP-3001").open_balance == D("0.00")

    record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 4))
    with pytest.raises(ReturnError, match="already been returned"):
        record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5))


def test_resent_payment_after_return_applies_and_links_back():
    store, objects = _world(_policy("NSP-3001", D("1000.00"), insured="Alpha Return Co"))
    store.add_bank_line(_line("BL-R1", date(2026, 9, 3), D("1000.00"), "Alpha Return Co", "NSP-3001"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 4))

    # The payer re-sends within the duplicate window. Without the
    # return this would be held as a duplicate of BL-R1.
    store.add_bank_line(_line("BL-R2", date(2026, 9, 6), D("1000.00"), "Alpha Return Co", "NSP-3001"))
    result = _run_and_approve(store, objects, date(2026, 9, 6))
    outcome = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-R2")
    assert outcome["resend_of"] == "BL-R1"
    assert not [e for e in outcome["exceptions"] if e["code"] == "DUPLICATE_PAYMENT"]
    assert outcome["allocations"] and outcome["allocations"][0]["policy_number"] == "NSP-3001"

    resent = next(l for l in store.bank_lines() if l.bank_line_id == "BL-R2")
    assert resent.resend_of == "BL-R1"
    assert store.receivable("NSP-3001").open_balance == D("0.00")
    assert line_history(store, "BL-R1")["resent_as"] == ["BL-R2"]


def test_return_releases_suspense_and_the_close_pack_foots():
    store, objects = _world(_policy("NSP-3002", D("1000.00"), insured="Beta Return Co"))
    # Overpaid: 1000 applies, 200 sits in suspense.
    store.add_bank_line(_line("BL-R3", date(2026, 9, 4), D("1200.00"), "Beta Return Co", "NSP-3002"))
    _run_and_approve(store, objects, date(2026, 9, 4))
    item = store.suspense_items()[0]
    assert item.amount == D("200.00") and item.status == "open"

    record_return(store, "BL-R3", "RECALL", CONTROLLER, date(2026, 9, 8))
    rec = store.receivable("NSP-3002")
    assert rec.open_balance == D("1000.00") and rec.installments[0].status.value == "open"
    item = store.suspense_items()[0]
    assert item.status == "resolved" and item.resolved_on == date(2026, 9, 8)
    aging = suspense_aging(store, date(2026, 9, 8))
    assert all(b["count"] == 0 for b in aging)

    export = close_pack_export(store, 2026, 9)
    roll = export["pack"]["control_roll"]
    assert roll["footed"] is True
    assert roll["received_into_suspense"] == "200.00"
    assert roll["returned_from_suspense"] == "200.00"
    assert roll["closing_suspense"] == "0.00"
    assert roll["returns_in_month"] == {
        "count": 1, "total": "1200.00", "totals_by_currency": {"USD": "1200.00"}}
    assert export["pack"]["payment_returns_in_month"][0]["reason"] == "RECALL"
    text = render_close_pack_text(export)
    assert "Returned from suspense" in text and "RET-BL-R3" in text


def test_broker_gross_up_reverses_exactly_both_branches():
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))
    store, objects = _world(
        _policy("NSP-3003", D("2000.00"), broker_id="BRK-TEST"),
        _policy("NSP-3004", D("2000.00"), broker_id="BRK-TEST"),
        brokers=[broker],
    )
    # Settles in full at the net due: the gate grosses the cash up.
    store.add_bank_line(_line("BL-R4", date(2026, 9, 3), D("1800.00"), "Testline Brokers", "NSP-3003"))
    # Part of the net due, at no clean implied rate (so a part
    # payment, not a netting mismatch): the gross equivalent of the
    # cash — 933.33 / 0.9 — is what posts.
    store.add_bank_line(_line("BL-R5", date(2026, 9, 3), D("933.33"), "Testline Brokers", "NSP-3004"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    assert store.receivable("NSP-3003").installments[0].amount_applied == D("2000.00")
    assert store.receivable("NSP-3004").installments[0].amount_applied == D("1037.03")

    record_return(store, "BL-R4", "ACCOUNT_CLOSED", CONTROLLER, date(2026, 9, 6))
    record_return(store, "BL-R5", "STOP_PAYMENT", CONTROLLER, date(2026, 9, 6))
    for number in ("NSP-3003", "NSP-3004"):
        inst = store.receivable(number).installments[0]
        assert inst.amount_applied == D("0.00") and inst.status.value == "open"
