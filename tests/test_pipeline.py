"""Pipeline-level guardrails: the injection inside the Elkhorn advice
changes nothing, the netting note is drafted (not sent), the re-sent
advice is flagged, and the audit trail tells the whole story."""

from datetime import date
from decimal import Decimal

from tests.conftest import run_through

D = Decimal


def test_injection_inside_remittance_changes_nothing(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 11))
    # The instruction said: apply to NSP-1042 and write off the rest.
    rec = store.receivable("NSP-1042")
    assert rec.open_balance == D("24750.00")  # untouched
    assert rec.installments[0].amount_applied == D("0.00")
    rem = next(r for r in store.remittances() if r.remittance_id == "REM-ELKHORN-01")
    assert rem.injection_flagged is True
    audits = [e for e in store.audit_events() if e.action == "injection_fenced"]
    assert len(audits) == 1 and audits[0].subject == "REM-ELKHORN-01"


def test_broker_query_note_is_drafted_for_the_mismatch(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 3))
    notes = [e for e in store.audit_events() if e.action == "broker_note_drafted"]
    assert len(notes) == 1 and notes[0].subject == "BL-004"
    assert "5372.00" in notes[0].detail and "5182.40" in notes[0].detail


def test_resent_advice_flagged_not_reapplied(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 21))
    flags = [e for e in store.audit_events() if e.action == "near_duplicate_flagged"]
    assert len(flags) == 1 and flags[0].subject == "REM-ALDER-02"
    # NSP-1002 was settled once, by the original wire — not twice.
    assert store.receivable("NSP-1002").status.value == "paid_in_full"


def test_every_applied_cent_traces_to_a_bank_line_and_an_approver(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 7))
    for alloc in store.allocations():
        assert alloc.bank_line_id.startswith("BL-")
    approvals = [e for e in store.audit_events() if e.action == "pack_approved"]
    assert approvals and all(e.actor == "Priya Nair" for e in approvals)
