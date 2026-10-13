"""Part-returns: the bank takes back part of a line (round 11).

Full-line returns are pinned in test_returns.py and unchanged.
These are the gold proofs for part-returns, all to the cent:

- a partial NSF on a broker netted line re-opens by the replayed
  gross-up — exactly, on both of the gate's branches;
- successive partials converge exactly: the pro-rata rounding of
  an early partial is absorbed by the last one, never accumulated;
- a partial on an EUR line reverses in EUR at the recorded
  conversion — the rate having moved since is neither here nor
  there, because nothing is re-priced;
- suspense unwinds before allocations: a small partial touches
  only suspense, and the close pack foots with partials inside;
- allocations unwind in reverse application order across a
  remittance pack's entries;
- an over-return is refused in words and changes nothing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon import gate
from cash_recon.close_pack import close_pack_export
from cash_recon.config import Policy as PolicyConfig
from cash_recon.line_export import build_line_export
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
from cash_recon.report import day_report, returns_view
from cash_recon.returns import ReturnError, line_history, record_return
from cash_recon.store import InMemoryStore

D = Decimal
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"
POLICY = PolicyConfig()


def _policy(number, premium, broker_id=None, insured=None, currency="USD",
            due=date(2026, 9, 1)):
    return Receivable(
        policy_number=number, insured_name=insured or f"Insured {number}",
        broker_id=broker_id, status=PolicyStatus.ACTIVE, currency=currency,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=due, amount_due=premium,
                                  currency=currency)],
    )


def _world(*receivables, brokers=()):
    store = InMemoryStore()
    for b in brokers:
        store.upsert_broker(b)
    for r in receivables:
        store.upsert_receivable(r)
    return store, InMemoryObjectStore()


def _run_and_approve(store, objects, day):
    result = run_day(store, objects, OfflineReader(), POLICY, day, actor=ASSEMBLER)
    if result["pack_id"]:
        gate.approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    return result


def _line(line_id, day, amount, payer, reference=None, currency="USD"):
    return BankLine(bank_line_id=line_id, value_date=day, amount=amount,
                    payer_name=payer, reference=reference, narrative="",
                    currency=currency)


def _inst(store, number):
    return store.receivable(number).installments[0]


# ---------------------------------------------------------------------------
# The netted partial: gross-up replay, cents exact


def test_partial_nsf_on_a_netted_line_reopens_exactly():
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers",
                    commission_rate=D("0.10"))
    store, objects = _world(
        _policy("NSP-3003", D("2000.00"), broker_id="BRK-TEST"), brokers=[broker])
    # 1800.00 cash settles the 2000.00 bill in full at the 10% net.
    store.add_bank_line(_line("BL-P1", date(2026, 9, 3), D("1800.00"),
                              "Testline Brokers", "NSP-3003"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    assert _inst(store, "NSP-3003").amount_applied == D("2000.00")

    ret = record_return(store, "BL-P1", "NSF", CONTROLLER, date(2026, 9, 5),
                        amount=D("450.00"))
    # A quarter of the cash came back; a quarter of the grossed-up
    # bill re-opens: 500.00, exactly — no rounding anywhere.
    assert ret.return_id == "RET-BL-P1" and ret.partial is True
    assert ret.amount == D("450.00") and ret.cumulative_returned == D("450.00")
    assert ret.suspense_released == D("0.00")
    assert [(u.cash, u.gross) for u in ret.unwind] == [(D("450.00"), D("500.00"))]
    inst = _inst(store, "NSP-3003")
    assert inst.amount_applied == D("1500.00")
    assert inst.status.value == "partially_applied"
    rec = store.receivable("NSP-3003")
    assert rec.open_balance == D("500.00") and rec.status == PolicyStatus.ACTIVE
    assert "part of BL-P1" in ret.detail and "still stands" in ret.detail
    # The pack is flagged, its entries untouched, as for a full return.
    assert store.pack("PACK-2026-09-03").flags == ["RETURN_AFFECTED"]

    # The rest comes back: a completing return on the remaining
    # balance behaves exactly as a full return on what stands.
    ret2 = record_return(store, "BL-P1", "NSF", CONTROLLER, date(2026, 9, 7))
    assert ret2.return_id == "RET-BL-P1-2" and ret2.partial is False
    assert ret2.amount == D("1350.00") and ret2.cumulative_returned == D("1800.00")
    assert [(u.cash, u.gross) for u in ret2.unwind] == [(D("1350.00"), D("1500.00"))]
    inst = _inst(store, "NSP-3003")
    assert inst.amount_applied == D("0.00") and inst.status.value == "open"
    assert store.receivable("NSP-3003").open_balance == D("2000.00")
    # The flag is raised once, not once per return.
    assert store.pack("PACK-2026-09-03").flags == ["RETURN_AFFECTED"]


def test_successive_partials_converge_to_the_cent():
    """The gate's awkward branch: 933.33 cash posted 1037.03 gross.
    Three equal partials of 311.11 must re-open 345.68, 345.68 and
    345.67 — the last absorbs the pro-rata rounding — summing to
    exactly 1037.03 and leaving the installment at exactly zero."""
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers",
                    commission_rate=D("0.10"))
    store, objects = _world(
        _policy("NSP-3004", D("2000.00"), broker_id="BRK-TEST"), brokers=[broker])
    store.add_bank_line(_line("BL-P2", date(2026, 9, 3), D("933.33"),
                              "Testline Brokers", "NSP-3004"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    assert _inst(store, "NSP-3004").amount_applied == D("1037.03")

    grosses = []
    for n, day in enumerate((date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7))):
        ret = record_return(store, "BL-P2", "NSF", CONTROLLER, day,
                            amount=D("311.11"))
        grosses.append(ret.unwind[0].gross)
        assert ret.return_id == (f"RET-BL-P2" if n == 0 else f"RET-BL-P2-{n + 1}")
    assert grosses == [D("345.68"), D("345.68"), D("345.67")]
    assert sum(grosses) == D("1037.03")
    inst = _inst(store, "NSP-3004")
    assert inst.amount_applied == D("0.00") and inst.status.value == "open"
    view = returns_view(store)
    assert view["count"] == 3 and view["total"] == "933.33"
    assert view["partial_count"] == 2 and view["partial_total"] == "622.22"


# ---------------------------------------------------------------------------
# The EUR partial: reversal in currency, at the recorded rate


def test_partial_return_on_an_eur_line_reverses_at_the_recorded_rate():
    broker = Broker(broker_id="BRK-EUR", name="Euro Test Brokers",
                    commission_rate=D("0.10"))
    store, objects = _world(
        _policy("NSP-E1", D("10000.00"), broker_id="BRK-EUR", currency="EUR",
                due=date(2026, 11, 1)),
        brokers=[broker])
    # Netted in EUR at the rate in force on the value date (1.08).
    store.add_bank_line(_line("BL-E1", date(2026, 11, 13), D("9000.00"),
                              "Euro Test Brokers", "NSP-E1", currency="EUR"))
    _run_and_approve(store, objects, date(2026, 11, 13))
    alloc = store.allocations("BL-E1")[0]
    assert alloc.fx_rate == D("1.08") and alloc.functional_amount == D("9720.00")
    assert _inst(store, "NSP-E1").amount_applied == D("10000.00")

    # The governed rate moves to 1.10 on Nov 16. The return on
    # Nov 20 reverses in EUR by the replayed gross-up — the move
    # is not re-priced into the reversal, in either direction.
    ret = record_return(store, "BL-E1", "NSF", CONTROLLER, date(2026, 11, 20),
                        amount=D("4500.00"))
    assert ret.currency == "EUR" and ret.partial is True
    assert [(u.cash, u.gross) for u in ret.unwind] == [(D("4500.00"), D("5000.00"))]
    inst = _inst(store, "NSP-E1")
    assert inst.amount_applied == D("5000.00") and inst.amount_due == D("10000.00")
    # The approved record stands untouched: the conversion on the
    # allocation is still the recorded one.
    alloc = store.allocations("BL-E1")[0]
    assert alloc.fx_rate == D("1.08") and alloc.functional_amount == D("9720.00")
    report = day_report(store, date(2026, 11, 20), POLICY)
    assert report["returns_recorded"]["totals_by_currency"] == {"EUR": "4500.00"}
    assert report["returns_recorded"]["partial_count"] == 1
    assert report["returns_recorded"]["partial_total"] == "4500.00"


# ---------------------------------------------------------------------------
# Suspense first, then allocations — and the close pack foots


def test_suspense_unwinds_first_and_the_close_pack_foots_with_partials():
    store, objects = _world(_policy("NSP-3002", D("1000.00"), insured="Beta Return Co"))
    # Overpaid: 1000.00 applies, 200.00 sits in suspense.
    store.add_bank_line(_line("BL-P3", date(2026, 9, 4), D("1200.00"),
                              "Beta Return Co", "NSP-3002"))
    _run_and_approve(store, objects, date(2026, 9, 4))
    item = store.suspense_items()[0]
    assert item.amount == D("200.00") and item.status == "open"

    # A 150.00 partial comes wholly out of suspense: the settled
    # bill is not disturbed at all.
    ret = record_return(store, "BL-P3", "RECALL", CONTROLLER, date(2026, 9, 8),
                        amount=D("150.00"))
    assert ret.suspense_released == D("150.00") and ret.unwind == []
    item = store.suspense_items()[0]
    assert item.amount == D("50.00") and item.status == "open"
    assert _inst(store, "NSP-3002").amount_applied == D("1000.00")

    # The next 100.00 takes the last 50.00 of suspense (the item
    # closes under the return) and 50.00 of the allocation.
    ret2 = record_return(store, "BL-P3", "RECALL", CONTROLLER, date(2026, 9, 10),
                         amount=D("100.00"))
    assert ret2.suspense_released == D("50.00")
    assert [(u.cash, u.gross) for u in ret2.unwind] == [(D("50.00"), D("50.00"))]
    item = store.suspense_items()[0]
    assert item.status == "resolved" and item.resolved_on == date(2026, 9, 10)
    assert _inst(store, "NSP-3002").amount_applied == D("950.00")

    export = close_pack_export(store, 2026, 9)
    roll = export["pack"]["control_roll"]
    assert roll["footed"] is True
    assert roll["received_into_suspense"] == "200.00"
    assert roll["returned_from_suspense"] == "200.00"
    assert roll["closing_suspense"] == "0.00"
    assert roll["returns_in_month"] == {
        "count": 2, "total": "250.00",
        "totals_by_currency": {"USD": "250.00"},
        "partial_count": 2, "partial_total": "250.00",
    }
    assert [r["partial"] for r in export["pack"]["payment_returns_in_month"]] == [True, True]


# ---------------------------------------------------------------------------
# Reverse application order across a pack's entries


def _advice(rem_id, day, lines, total):
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
    return Remittance(remittance_id=rem_id, broker_id="BRK-TEST",
                      received_date=date.fromisoformat(day), stated_total=None,
                      text="\n".join(body))


def test_allocations_unwind_in_reverse_application_order():
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers",
                    commission_rate=D("0.10"))
    store, objects = _world(
        _policy("NSP-2001", D("2000.00"), broker_id="BRK-TEST"),
        _policy("NSP-2002", D("3000.00"), broker_id="BRK-TEST"),
        brokers=[broker])
    store.add_remittance(_advice("REM-P1", "2026-09-05",
                                  [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))],
                                  D("4500.00")))
    store.add_bank_line(_line("BL-P4", date(2026, 9, 5), D("4500.00"),
                              "Testline Brokers", None))
    _run_and_approve(store, objects, date(2026, 9, 5))
    assert _inst(store, "NSP-2001").amount_applied == D("2000.00")
    assert _inst(store, "NSP-2002").amount_applied == D("3000.00")

    # 3000.00 back: the LAST application (NSP-2002, 2700.00 cash)
    # unwinds whole first; the remainder takes 300.00 of NSP-2001's
    # cash, re-opening 333.33 of its grossed-up bill.
    ret = record_return(store, "BL-P4", "NSF", CONTROLLER, date(2026, 9, 7),
                        amount=D("3000.00"))
    assert [(u.policy_number, u.cash, u.gross) for u in ret.unwind] == [
        ("NSP-2002", D("2700.00"), D("3000.00")),
        ("NSP-2001", D("300.00"), D("333.33")),
    ]
    assert _inst(store, "NSP-2002").amount_applied == D("0.00")
    assert _inst(store, "NSP-2002").status.value == "open"
    assert _inst(store, "NSP-2001").amount_applied == D("1666.67")

    # Completing the line re-opens exactly what is left — the
    # residual of the pro-rata rounding, to the cent.
    ret2 = record_return(store, "BL-P4", "NSF", CONTROLLER, date(2026, 9, 9))
    assert [(u.cash, u.gross) for u in ret2.unwind] == [(D("1500.00"), D("1666.67"))]
    assert _inst(store, "NSP-2001").amount_applied == D("0.00")


# ---------------------------------------------------------------------------
# The over-return refusal, and the export's partly-returned state


def test_an_over_return_is_refused_in_words_and_changes_nothing():
    store, objects = _world(_policy("NSP-3001", D("1000.00"), insured="Alpha Return Co"))
    store.add_bank_line(_line("BL-P5", date(2026, 9, 3), D("1000.00"),
                              "Alpha Return Co", "NSP-3001"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    record_return(store, "BL-P5", "NSF", CONTROLLER, date(2026, 9, 5),
                  amount=D("600.00"))
    assert _inst(store, "NSP-3001").amount_applied == D("400.00")

    with pytest.raises(ReturnError, match="only 400.00 of the line's 1000.00"):
        record_return(store, "BL-P5", "NSF", CONTROLLER, date(2026, 9, 6),
                      amount=D("500.00"))
    with pytest.raises(ReturnError, match="must be positive"):
        record_return(store, "BL-P5", "NSF", CONTROLLER, date(2026, 9, 6),
                      amount=D("0.00"))
    # Nothing moved: one return on the record, the same balance.
    assert len(store.returns_for_line("BL-P5")) == 1
    assert _inst(store, "NSP-3001").amount_applied == D("400.00")

    record_return(store, "BL-P5", "NSF", CONTROLLER, date(2026, 9, 7))
    assert _inst(store, "NSP-3001").amount_applied == D("0.00")
    history = line_history(store, "BL-P5")
    assert [r["return_id"] for r in history["returns"]] == ["RET-BL-P5", "RET-BL-P5-2"]
    assert history["return"]["return_id"] == "RET-BL-P5"


def test_the_line_export_names_a_partly_returned_line_and_foots():
    store, objects = _world(_policy("NSP-3001", D("1000.00"), insured="Alpha Return Co"))
    store.add_bank_line(_line("BL-P6", date(2026, 9, 3), D("1000.00"),
                              "Alpha Return Co", "NSP-3001"))
    _run_and_approve(store, objects, date(2026, 9, 3))
    record_return(store, "BL-P6", "NSF", CONTROLLER, date(2026, 9, 5),
                  amount=D("400.00"))

    body = build_line_export(store, "BL-P6")
    assert body["final_state"]["state"] == "partly returned"
    assert body["final_state"]["returned"] == "400.00"
    assert body["return"]["return_id"] == "RET-BL-P6"
    assert "returns" not in body  # one return: the single-record shape stands
    footing = body["footing"]
    assert footing["allocations_posted"] == "1000.00"
    assert footing["allocations_reversed_by_return"] == "400.00"
    assert footing["returned_amount"] == "400.00"
    assert footing["foots"] is True

    record_return(store, "BL-P6", "NSF", CONTROLLER, date(2026, 9, 6))
    body = build_line_export(store, "BL-P6")
    assert body["final_state"]["state"] == "returned"
    assert "across 2 returns" in body["final_state"]["detail"]
    assert [r["return_id"] for r in body["returns"]] == ["RET-BL-P6", "RET-BL-P6-2"]
    assert body["footing"]["allocations_reversed_by_return"] == "1000.00"
    assert body["footing"]["foots"] is True
