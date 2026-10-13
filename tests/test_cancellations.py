"""Return premiums: the cancellation computation, the payable it
opens, and the payout pack that settles it at the gate.

The arithmetic is pinned to the cent by construction: a 12,000.00
premium over a 365-day term, cancelled 91 days in with 6,000.00
paid, earns 2,991.78 and returns 3,008.22."""

from datetime import date
from decimal import Decimal

import pytest

from cash_recon.cancellations import (
    CancellationError,
    pro_rata_earned,
    record_cancellation,
)
from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import GateError
from cash_recon.models import (
    Broker,
    Installment,
    PolicyStatus,
    Receivable,
)
from cash_recon.payouts import (
    approve_payout_pack,
    assemble_payout_pack,
    payout_file,
    reject_payout_pack,
)
from cash_recon.store import InMemoryStore

D = Decimal
CONTROLLER = "Priya Nair"
SECOND = "Sofia Marino"


def _policy(number, premium, installments=1, broker_id=None, paid=(),
            term=(date(2026, 9, 1), date(2027, 9, 1))):
    per = premium / installments
    insts = []
    for i in range(installments):
        inst = Installment(installment_id=f"{number}-I{i + 1}", policy_number=number,
                           sequence=i + 1, due_date=date(2026, 9, 1), amount_due=per)
        if (i + 1) in paid:
            inst.amount_applied = per
            inst.status = "applied"
        insts.append(inst)
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE, installments=insts,
        term_start=term[0] if term else None, term_end=term[1] if term else None,
    )


def _store_with(rec, broker=None):
    store = InMemoryStore()
    if broker:
        store.upsert_broker(broker)
    store.upsert_receivable(rec)
    return store


def test_pro_rata_earned_is_exact():
    earned, term_days, elapsed = pro_rata_earned(
        D("12000.00"), date(2026, 9, 1), date(2027, 9, 1), date(2026, 12, 1))
    assert (earned, term_days, elapsed) == (D("2991.78"), 365, 91)
    earned, term_days, elapsed = pro_rata_earned(
        D("9000.00"), date(2026, 9, 1), date(2027, 3, 1), date(2026, 11, 1))
    assert (earned, term_days, elapsed) == (D("3033.15"), 181, 61)


def test_cancellation_computes_the_return_and_opens_the_payable():
    store = _store_with(_policy("NSP-6001", D("12000.00"), installments=4, paid=(1, 2)))
    cxl = record_cancellation(store, "NSP-6001", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1),
                              reason="insured's request")
    assert cxl.earned_premium == D("2991.78") and cxl.paid_to_date == D("6000.00")
    assert cxl.return_premium == D("3008.22") and cxl.insured_owes == D("0.00")
    assert cxl.payee_kind == "insured" and cxl.payee_name == "Insured NSP-6001"
    # The computation is on the record, line by line.
    assert any("3008.22" in line for line in cxl.computation)
    assert any("91/365" in line for line in cxl.computation)
    payable = store.payable_for_policy("NSP-6001")
    assert payable.amount == D("3008.22") and payable.remaining == D("3008.22")
    assert payable.status == "open" and payable.payee_kind == "insured"
    assert [e.kind for e in payable.events] == ["opened"]
    assert store.receivable("NSP-6001").status == PolicyStatus.CANCELLED


def test_the_payable_goes_to_the_broker_who_collected():
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))
    rec = _policy("NSP-6002", D("9000.00"), broker_id="BRK-TEST", paid=(1,),
                  term=(date(2026, 9, 1), date(2027, 3, 1)))
    store = _store_with(rec, broker)
    cxl = record_cancellation(store, "NSP-6002", date(2026, 11, 1), CONTROLLER, date(2026, 11, 1))
    assert cxl.return_premium == D("5966.85")
    assert cxl.payee_kind == "broker" and cxl.payee_name == "Testline Brokers"
    assert store.payable_for_policy("NSP-6002").broker_id == "BRK-TEST"


def test_an_insured_truly_behind_opens_no_payable():
    # Two installments billed, none paid, cancelled 91 days in:
    # earned 2991.78, paid 0 — the insured owes, no payable opens.
    store = _store_with(_policy("NSP-6004", D("12000.00"), installments=4, paid=()))
    cxl = record_cancellation(store, "NSP-6004", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    assert cxl.return_premium == D("0.00") and cxl.insured_owes == D("2991.78")
    assert cxl.payable_id is None
    assert store.payable_for_policy("NSP-6004") is None


def test_cancellation_refusals_are_named():
    store = _store_with(_policy("NSP-6005", D("12000.00"), installments=4, paid=(1, 2)))
    with pytest.raises(CancellationError, match="no such policy"):
        record_cancellation(store, "NSP-9999", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    with pytest.raises(CancellationError, match="named person"):
        record_cancellation(store, "NSP-6005", date(2026, 12, 1), "", date(2026, 12, 1))
    with pytest.raises(CancellationError, match="outside the policy term"):
        record_cancellation(store, "NSP-6005", date(2027, 9, 1), CONTROLLER, date(2026, 12, 1))
    record_cancellation(store, "NSP-6005", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    with pytest.raises(CancellationError, match="already cancelled"):
        record_cancellation(store, "NSP-6005", date(2026, 12, 2), CONTROLLER, date(2026, 12, 2))

    bare = _store_with(_policy("NSP-6006", D("12000.00"), installments=4, paid=(1,), term=None))
    with pytest.raises(CancellationError, match="no term dates"):
        record_cancellation(bare, "NSP-6006", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))


def _payable_world(amount_paid=(1, 2)):
    store = _store_with(_policy("NSP-6001", D("12000.00"), installments=4, paid=amount_paid))
    record_cancellation(store, "NSP-6001", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    return store


def test_payout_pack_pays_the_payable_under_the_gate_rules():
    store = _payable_world()
    pack = assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6001"], "cash-team")
    assert pack.total == D("3008.22") and pack.status.value == "proposed"
    with pytest.raises(GateError, match="payout file exists only"):
        payout_file(pack)
    with pytest.raises(GateError, match="self-approval refused"):
        approve_payout_pack(store, pack.pack_id, "cash-team", PolicyConfig())
    # 3008.22 is above the 250.00 single-approver limit: one
    # approver is not enough, and the second must be a third person.
    with pytest.raises(GateError, match="second approver is required"):
        approve_payout_pack(store, pack.pack_id, CONTROLLER, PolicyConfig())
    with pytest.raises(GateError, match="second approver must differ"):
        approve_payout_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(), second_approver=CONTROLLER)
    approved = approve_payout_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                                   second_approver=SECOND)
    assert approved.approvals == [CONTROLLER, SECOND]
    payable = store.payable("PAY-NSP-6001")
    assert payable.status == "paid" and payable.remaining == D("0.00")
    assert [e.kind for e in payable.events] == ["opened", "paid"]
    text = payout_file(approved)
    assert "PAY-NSP-6001" in text and "3008.22" in text and "insured" in text


def test_a_small_payout_needs_one_approver():
    store = _store_with(_policy("NSP-6007", D("12000.00"), installments=4, paid=(1,)))
    cxl = record_cancellation(store, "NSP-6007", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    assert cxl.return_premium == D("8.22")  # under the limit
    pack = assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6007"], "cash-team")
    approve_payout_pack(store, pack.pack_id, CONTROLLER, PolicyConfig())
    assert store.payable("PAY-NSP-6007").status == "paid"


def test_a_payable_that_moved_since_assembly_refuses_the_pack():
    store = _payable_world()
    pack = assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6001"], "cash-team")
    payable = store.payable("PAY-NSP-6001")
    payable.amount_netted = D("1000.00")  # cash netted against it meanwhile
    store.save_payable(payable)
    with pytest.raises(GateError, match="moved since the pack was assembled"):
        approve_payout_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(), second_approver=SECOND)


def test_payout_assembly_refusals():
    store = _payable_world()
    with pytest.raises(GateError, match="no such payable"):
        assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NOPE"], "cash-team")
    assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6001"], "cash-team")
    with pytest.raises(GateError, match="already on a proposed payout pack"):
        assemble_payout_pack(store, date(2026, 12, 5), ["PAY-NSP-6001"], "cash-team")
    pack = store.payout_packs()[0]
    with pytest.raises(GateError, match="other than the assembler"):
        reject_payout_pack(store, pack.pack_id, "cash-team", "not today")
    rejected = reject_payout_pack(store, pack.pack_id, CONTROLLER, "Payee details unconfirmed.")
    assert rejected.status.value == "rejected"
