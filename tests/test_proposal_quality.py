"""Model proposal quality: the gate grades the model by what the
approver actually did.

Pins: provenance is stamped at assembly (remittance reads and
lump-sum proposals only — code tiers propose nothing); the
edit-then-approve path is governed (named approver-side editor,
real open targets, per-line footing or the whole edit is refused);
and the quality report's figures are derived from the pack records
— one of each outcome, footing to the gate's own record.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon import gate
from cash_recon.close_pack import close_pack_export
from cash_recon.config import Policy as PolicyConfig
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
from cash_recon.report import proposal_quality
from cash_recon.store import InMemoryStore

D = Decimal
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"
BROKER = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))


def _policy(number, premium, broker_id="BRK-TEST"):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


_NAMES = {"NSP-2001": "Alpha Test Co", "NSP-2002": "Beta Test Co"}


def _advice(day, lines, total):
    body = [
        "Remittance advice — Testline Brokers",
        "Broker account: BRK-TEST",
        f"Date: {day}",
        "",
        "Premium received for the following policies (net of commission):",
        *[f"Policy {p} — {_NAMES[p]} — {amt:,.2f}" for p, amt in lines],
        "",
        f"Total remitted: {total:,.2f}",
    ]
    return "\n".join(body)


def _remittance_world():
    store = InMemoryStore()
    store.upsert_broker(BROKER)
    store.upsert_receivable(_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(_policy("NSP-2002", D("3000.00")))
    store.add_remittance(Remittance(
        remittance_id="REM-Q1", broker_id="BRK-TEST", received_date=date(2026, 9, 5),
        stated_total=None,
        text=_advice("2026-09-05", [("NSP-2001", D("1800.00")), ("NSP-2002", D("2700.00"))], D("4500.00"))))
    store.add_bank_line(BankLine(bank_line_id="BL-Q1", value_date=date(2026, 9, 5),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    result = run_day(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig(),
                     date(2026, 9, 5), actor=ASSEMBLER)
    return store, result["pack_id"]


def _lump_sum_world():
    store = InMemoryStore()
    store.upsert_broker(BROKER)
    store.upsert_receivable(_policy("NSP-5001", D("1000.00")))
    store.upsert_receivable(_policy("NSP-5002", D("2000.00")))
    # No reference, no advice: the proposer must find 900 + 1800.
    store.add_bank_line(BankLine(bank_line_id="BL-Q9", value_date=date(2026, 9, 5),
                                 amount=D("2700.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire"))
    result = run_day(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig(),
                     date(2026, 9, 5), actor=ASSEMBLER)
    return store, result["pack_id"]


def test_provenance_is_stamped_at_assembly():
    store, pack_id = _remittance_world()
    pack = store.pack(pack_id)
    assert len(pack.entries) == 2
    assert {e.proposal_kind for e in pack.entries} == {"remittance_read"}
    assert {e.proposed_by for e in pack.entries} == {"offline"}

    store2, pack_id2 = _lump_sum_world()
    pack2 = store2.pack(pack_id2)
    assert len(pack2.entries) == 2
    assert {e.proposal_kind for e in pack2.entries} == {"lump_sum"}
    assert {e.proposed_by for e in pack2.entries} == {"offline"}


def test_edit_then_approve_is_governed_and_footed():
    store, pack_id = _remittance_world()
    pack = store.pack(pack_id)
    idx = {e.allocation.policy_number: i for i, e in enumerate(pack.entries)}

    # The assembler cannot edit their own pack.
    with pytest.raises(gate.GateError, match="not the assembler"):
        gate.edit_pack_entries(store, pack_id, ASSEMBLER,
                               [{"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")}])
    # An unbalanced amount change breaks the line's footing: refused
    # whole, and the stored pack is untouched.
    with pytest.raises(gate.GateError, match="footing for bank line BL-Q1"):
        gate.edit_pack_entries(store, pack_id, CONTROLLER,
                               [{"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")}])
    assert all(not e.edits for e in store.pack(pack_id).entries)
    # A target that does not exist is refused.
    with pytest.raises(gate.GateError, match="no policy NSP-9999"):
        gate.edit_pack_entries(store, pack_id, CONTROLLER,
                               [{"entry_index": idx["NSP-2001"], "new_policy": "NSP-9999",
                                 "new_installment": "NSP-9999-I1"}])

    edited = gate.edit_pack_entries(store, pack_id, CONTROLLER, [
        {"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")},
        {"entry_index": idx["NSP-2002"], "new_amount": D("2800.00")},
    ])
    first = edited.entries[idx["NSP-2001"]]
    assert first.allocation.amount == D("1700.00")
    assert first.edits[0].before_amount == D("1800.00") and first.edits[0].by == CONTROLLER
    # The allocation index follows the edited pack.
    amounts = {a.policy_number: a.amount for a in store.allocations("BL-Q1")}
    assert amounts == {"NSP-2001": D("1700.00"), "NSP-2002": D("2800.00")}

    gate.approve_pack(store, pack_id, CONTROLLER, PolicyConfig())
    with pytest.raises(gate.GateError, match="only a proposed pack can be edited"):
        gate.edit_pack_entries(store, pack_id, CONTROLLER,
                               [{"entry_index": 0, "new_amount": D("1.00")}])

    quality = proposal_quality(store)
    assert quality["by_kind"]["remittance_read"] == {
        "proposals": 2, "accepted_as_proposed": 0, "accepted_with_edits": 2,
        "rejected": 0, "value_edited": "200.00",
    }
    assert quality["totals"]["value_edited"] == "200.00"
    outcomes = {(d["policy_number"], d["outcome"]) for d in quality["proposals"]}
    assert outcomes == {("NSP-2001", "ACCEPTED_WITH_EDITS"), ("NSP-2002", "ACCEPTED_WITH_EDITS")}


def test_retarget_edit_counts_the_redirected_value():
    store, pack_id = _remittance_world()
    pack = store.pack(pack_id)
    idx = {e.allocation.policy_number: i for i, e in enumerate(pack.entries)}
    # Redirect NSP-2001's whole allocation onto NSP-2002's second
    # installment — first give NSP-2002 a second installment to aim at.
    rec = store.receivable("NSP-2002")
    rec.installments.append(Installment(
        installment_id="NSP-2002-I2", policy_number="NSP-2002", sequence=2,
        due_date=date(2026, 10, 1), amount_due=D("500.00")))
    store.upsert_receivable(rec)
    gate.edit_pack_entries(store, pack_id, CONTROLLER, [
        {"entry_index": idx["NSP-2001"], "new_policy": "NSP-2002", "new_installment": "NSP-2002-I2"},
    ])
    gate.approve_pack(store, pack_id, CONTROLLER, PolicyConfig())
    quality = proposal_quality(store)
    moved = next(d for d in quality["proposals"] if d["installment_id"] == "NSP-2002-I2")
    assert moved["outcome"] == "ACCEPTED_WITH_EDITS"
    assert moved["edited_value"] == "1800.00"  # the full redirected amount
    assert moved["original_amount"] == "1800.00"


def test_lump_sum_accepted_as_proposed_and_rejected_pack_grades_rejected():
    store, pack_id = _lump_sum_world()
    gate.approve_pack(store, pack_id, CONTROLLER, PolicyConfig())
    quality = proposal_quality(store)
    assert quality["by_kind"]["lump_sum"] == {
        "proposals": 2, "accepted_as_proposed": 2, "accepted_with_edits": 0,
        "rejected": 0, "value_edited": "0.00",
    }

    store2, pack_id2 = _lump_sum_world()
    gate.reject_pack(store2, pack_id2, CONTROLLER, "The subset looks wrong; ask the broker.")
    quality2 = proposal_quality(store2)
    assert quality2["by_kind"]["lump_sum"]["rejected"] == 2
    assert quality2["totals"]["proposals"] == 2


def test_quality_figures_foot_to_the_gate_records_and_close_pack_carries_them():
    store, pack_id = _remittance_world()
    store2, pack_id2 = _lump_sum_world()
    # Merge world 2's decided proposals into the same books by
    # replaying its pack: simpler to grade each world and check the
    # identity within one combined run — use the remittance world
    # plus its own lump-sum day instead.
    gate.approve_pack(store, pack_id, CONTROLLER, PolicyConfig())
    quality = proposal_quality(store)
    decided_proposals = sum(
        1 for p in store.packs() if p.status.value in ("approved", "rejected")
        for e in p.entries if e.proposal_kind
    )
    assert quality["totals"]["proposals"] == decided_proposals == len(quality["proposals"])
    kinds_sum = sum(v["proposals"] for v in quality["by_kind"].values())
    assert kinds_sum == quality["totals"]["proposals"]
    outcomes_sum = sum(
        v["accepted_as_proposed"] + v["accepted_with_edits"] + v["rejected"]
        for v in quality["by_kind"].values()
    )
    assert outcomes_sum == quality["totals"]["proposals"]

    export = close_pack_export(store, 2026, 9)
    scoped = export["pack"]["proposal_quality"]
    assert scoped["totals"]["proposals"] == 2
    assert scoped["by_kind"]["remittance_read"]["accepted_as_proposed"] == 2
    assert proposal_quality(store, month=(2026, 10))["totals"]["proposals"] == 0
