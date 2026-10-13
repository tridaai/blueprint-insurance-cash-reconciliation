"""Combination settlements: two wires from the same payer that
together settle one open installment exactly.

The pass is code, not model: a pair is proposed only when each leg
priced at the commission rate in force on its own value date sums
to the target's open billed balance to the cent. These tests pin
the proof (exact, per-leg rates, same payer only), the governance
(proposal at the gate, atomic approval, rejection frees the legs,
no edits), and the refusals (a cent off, across payers, a target
that moved since proposal).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from cash_recon import combinations, gate
from cash_recon.config import Policy as PolicyConfig
from cash_recon.models import (
    BankLine,
    Broker,
    ExceptionCode,
    ExceptionRecord,
    Installment,
    PolicyStatus,
    RatePeriod,
    Receivable,
    SuspenseItem,
)
from cash_recon.report import proposal_quality
from cash_recon.store import InMemoryStore

ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def _policy(number, insured, broker_id, premium):
    return Receivable(
        policy_number=number, insured_name=insured, broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 10, 1),
                                  amount_due=D(premium))],
    )


def _park(store, line_id, day, amount, payer):
    """A wholly unapplied line in suspense, the way the tiers leave
    an unidentified wire."""
    store.add_bank_line(BankLine(
        bank_line_id=line_id, value_date=day, amount=D(amount),
        payer_name=payer, reference=None, narrative="premium part payment",
    ))
    store.save_suspense(SuspenseItem(
        suspense_id=f"SUS-{line_id}", bank_line_id=line_id, amount=D(amount),
        reason="UNIDENTIFIED", received_date=day,
    ))
    store.update_exception(ExceptionRecord(
        exception_id=f"EXC-{line_id}", code=ExceptionCode.UNIDENTIFIED,
        bank_line_id=line_id, policy_number=None, broker_id=None,
        amount=D(amount), detail="no explanation", first_seen=day,
    ))


def _gable_world():
    store = InMemoryStore()
    store.upsert_broker(Broker(
        broker_id="BRK-GABLE", name="Gable & Rowe Brokers", commission_rate=D("0.09")))
    store.upsert_receivable(_policy("NSP-9001", "Kestrel Point Marina", "BRK-GABLE", "6000.00"))
    _park(store, "BL-A", date(2026, 10, 6), "2900.00", "Gable & Rowe Brokers")
    _park(store, "BL-B", date(2026, 10, 27), "2560.00", "Gable & Rowe Brokers")
    return store


def test_exact_pair_is_proposed_with_both_legs_itemised():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    assert summary["pack_id"] == "PACK-COMB-2026-10-27"
    (combo,) = summary["proposed"]
    assert combo["installment_id"] == "NSP-9001-I1"
    assert combo["cash_total"] == "5460.00"
    pack = store.pack(summary["pack_id"])
    assert pack.status.value == "proposed"
    assert len(pack.entries) == 2
    assert all(e.proposal_kind == "combination" for e in pack.entries)
    assert all(e.proposed_by == "combination-pass" for e in pack.entries)
    assert {e.combination_id for e in pack.entries} == {"COMB-BL-A+BL-B"}
    # The legs are held from suspense at proposal, like a re-match
    # recovery — the cash is spoken for, and the day still foots.
    items = {i.bank_line_id: i for i in store.suspense_items()}
    assert items["BL-A"].status == "resolved"
    assert items["BL-B"].resolved_on == date(2026, 10, 27)
    # Nothing applied yet: proposals stop at the gate.
    assert store.receivable("NSP-9001").open_balance == D("6000.00")


def test_approval_applies_both_legs_and_settles_the_bill_exactly():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    gate.approve_pack(store, summary["pack_id"], CONTROLLER, PolicyConfig())
    rec = store.receivable("NSP-9001")
    inst = rec.installments[0]
    assert inst.amount_applied == D("6000.00")  # billed, to the cent
    assert rec.open_balance == D("0.00")
    assert rec.status == PolicyStatus.PAID_IN_FULL
    # The legs' exceptions close with the settlement.
    exc = {e.bank_line_id: e for e in store.exceptions()}
    assert exc["BL-A"].status == "resolved"
    assert exc["BL-B"].status == "resolved"


def test_rate_straddle_pair_prices_each_leg_at_its_own_rate():
    store = InMemoryStore()
    store.upsert_broker(Broker(
        broker_id="BRK-FERN", name="Fernway Insurance Group",
        commission_rate=D("0.13"),
        rate_history=[
            RatePeriod(effective_from=date(2026, 1, 1), rate=D("0.11")),
            RatePeriod(effective_from=date(2026, 10, 15), rate=D("0.13")),
        ]))
    store.upsert_receivable(_policy("NSP-9002", "Halloran Freight Lines", "BRK-FERN", "10000.00"))
    _park(store, "BL-C", date(2026, 10, 8), "3738.00", "Fernway Insurance Group")
    _park(store, "BL-D", date(2026, 10, 22), "5046.00", "Fernway Insurance Group")
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 22))
    assert summary["pack_id"] == "PACK-COMB-2026-10-22"
    gate.approve_pack(store, summary["pack_id"], CONTROLLER, PolicyConfig())
    rec = store.receivable("NSP-9002")
    # 3738/0.89 = 4200.00 and 5046/0.87 = 5800.00: applied exactly.
    assert rec.installments[0].amount_applied == D("10000.00")
    assert rec.open_balance == D("0.00")


def test_cent_off_pair_is_never_proposed():
    store = InMemoryStore()
    store.upsert_broker(Broker(
        broker_id="BRK-BAY", name="Bayline Risk Partners", commission_rate=D("0.10")))
    store.upsert_receivable(_policy("NSP-9003", "Pemberton Auctions", "BRK-BAY", "5000.00"))
    _park(store, "BL-E", date(2026, 10, 9), "2000.00", "Bayline Risk Partners")
    _park(store, "BL-F", date(2026, 10, 23), "2499.99", "Bayline Risk Partners")
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 23))
    assert summary == {"proposed": [], "pack_id": None}
    assert all(i.status == "open" for i in store.suspense_items())


def test_exact_sum_across_payers_is_never_proposed():
    store = InMemoryStore()
    store.upsert_broker(Broker(
        broker_id="BRK-COPPER", name="Copperline Insurance Services",
        commission_rate=D("0.15")))
    store.upsert_broker(Broker(
        broker_id="BRK-ALDER", name="Alder & Voss Insurance Brokers",
        commission_rate=D("0.08")))
    store.upsert_receivable(_policy("NSP-9004", "Lowmarsh Oyster Farms", "BRK-COPPER", "4000.00"))
    # 1700 + 1700 = 3400 = the bill's net due — but the wires are
    # from two other payers entirely. Neither is the policy's broker.
    _park(store, "BL-G", date(2026, 10, 12), "1700.00", "Alder & Voss Insurance Brokers")
    _park(store, "BL-H", date(2026, 10, 19), "1700.00", "Harborlight Insurance Advisors")
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 19))
    assert summary["proposed"] == []


def test_direct_pair_settles_without_any_commission():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-9005", "Wexford Tutoring LLC", None, "3150.00"))
    _park(store, "BL-I", date(2026, 10, 3), "2000.00", "Wexford Tutoring LLC")
    _park(store, "BL-J", date(2026, 10, 20), "1150.00", "Wexford Tutoring LLC")
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 20))
    assert summary["pack_id"] == "PACK-COMB-2026-10-20"
    gate.approve_pack(store, summary["pack_id"], CONTROLLER, PolicyConfig())
    assert store.receivable("NSP-9005").open_balance == D("0.00")


def test_rejection_frees_the_legs_and_the_pair_can_be_proposed_again():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    gate.reject_pack(store, summary["pack_id"], CONTROLLER, "checking with the broker first")
    items = {i.bank_line_id: i for i in store.suspense_items()}
    assert items["BL-A"].status == "open" and items["BL-A"].resolved_on is None
    assert items["BL-B"].status == "open"
    again = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 28))
    assert again["proposed"], "freed legs must be proposable again"
    assert again["pack_id"] == "PACK-COMB-2026-10-28"


def test_committed_legs_are_not_reproposed():
    store = _gable_world()
    combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    again = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 28))
    assert again["proposed"] == []


def test_approval_refused_whole_when_the_target_moved_since_proposal():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    # Another act settles part of the bill before the gate decides.
    rec = store.receivable("NSP-9001")
    rec.installments[0].amount_applied = D("1000.00")
    store.upsert_receivable(rec)
    with pytest.raises(gate.GateError, match="no longer settles"):
        gate.approve_pack(store, summary["pack_id"], CONTROLLER, PolicyConfig())
    # And nothing was applied by the refused approval.
    assert store.receivable("NSP-9001").installments[0].amount_applied == D("1000.00")


def test_combination_pack_cannot_be_edited_at_the_gate():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    with pytest.raises(gate.GateError, match="combination"):
        gate.edit_pack_entries(store, summary["pack_id"], CONTROLLER, [
            {"entry": 0, "amount": "2900.00", "policy_number": "NSP-9001",
             "installment_id": "NSP-9001-I1"},
        ])


def test_proposal_quality_grades_the_combination_kind():
    store = _gable_world()
    summary = combinations.propose_combinations(store, ASSEMBLER, date(2026, 10, 27))
    gate.approve_pack(store, summary["pack_id"], CONTROLLER, PolicyConfig())
    quality = proposal_quality(store)
    assert quality["by_kind"]["combination"]["proposals"] == 2
    assert quality["by_kind"]["combination"]["accepted_as_proposed"] == 2
