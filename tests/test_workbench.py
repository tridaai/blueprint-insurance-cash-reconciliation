"""The exception workbench: assignment, append-only notes, and a
view whose counts foot to the queue.

Assignment and notes are handling metadata — the pins that matter:
they persist with attribution, they refuse names off the roster,
they never touch the exception's amount, code, or disposition, and
there is no path that edits or deletes a note once written.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.models import BankLine
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.store import InMemoryStore
from cash_recon.workbench import WorkbenchError, add_handling_note, assign_exception, workbench_view

D = Decimal


def _store_with_exceptions():
    """A small world with two open exceptions: an unidentified line
    and a partial payment."""
    from cash_recon.models import Installment, PolicyStatus, Receivable

    store = InMemoryStore()
    store.upsert_receivable(Receivable(
        policy_number="NSP-4001", insured_name="Gamma Workbench Co", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-4001-I1", policy_number="NSP-4001",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("1000.00"))]))
    store.add_bank_line(BankLine(bank_line_id="BL-W1", value_date=date(2026, 9, 2),
                                 amount=D("500.00"), payer_name="Stranger", reference=None,
                                 narrative=""))
    store.add_bank_line(BankLine(bank_line_id="BL-W2", value_date=date(2026, 9, 3),
                                 amount=D("400.00"), payer_name="Gamma Workbench Co",
                                 reference="NSP-4001", narrative=""))
    objects = InMemoryObjectStore()
    run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 2), actor="cash-team")
    run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 3), actor="cash-team")
    return store


def test_assignment_persists_attributed_and_touches_nothing_else():
    store = _store_with_exceptions()
    exc = next(e for e in store.exceptions(status="open") if e.code.value == "UNIDENTIFIED")
    before = (exc.amount, exc.code, exc.status, exc.detail)

    assign_exception(store, exc.exception_id, "Mei-Ling Chen", actor="cash-team")
    after = next(e for e in store.exceptions() if e.exception_id == exc.exception_id)
    assert after.assignee == "Mei-Ling Chen"
    assert (after.amount, after.code, after.status, after.detail) == before

    events = [e for e in store.audit_events(exc.exception_id) if e.action == "exception_assigned"]
    assert events and events[0].actor == "cash-team" and "Mei-Ling Chen" in events[0].detail


def test_assignment_refuses_off_roster_names_and_closed_work():
    store = _store_with_exceptions()
    exc = store.exceptions(status="open")[0]
    with pytest.raises(WorkbenchError, match="not on the cash team roster"):
        assign_exception(store, exc.exception_id, "Some Outsider", actor="cash-team")
    exc.status = "resolved"
    store.update_exception(exc)
    with pytest.raises(WorkbenchError, match="assigns open work"):
        assign_exception(store, exc.exception_id, "Priya Nair", actor="cash-team")
    with pytest.raises(WorkbenchError, match="no such exception"):
        assign_exception(store, "EXC-NOPE", "Priya Nair", actor="cash-team")


def test_notes_are_append_only_and_attributed():
    store = _store_with_exceptions()
    exc = store.exceptions(status="open")[0]
    add_handling_note(store, exc.exception_id, "Tom Alvarez", "Chased the broker by email.")
    add_handling_note(store, exc.exception_id, "Priya Nair", "Broker says the advice follows Friday.")
    after = next(e for e in store.exceptions() if e.exception_id == exc.exception_id)
    assert [n.by for n in after.handling_notes] == ["Tom Alvarez", "Priya Nair"]
    assert [n.text for n in after.handling_notes] == [
        "Chased the broker by email.", "Broker says the advice follows Friday."]
    assert all(n.at is not None for n in after.handling_notes)
    # The module surface offers no edit or delete for notes.
    import cash_recon.workbench as wb

    assert not any(hasattr(wb, name) for name in ("edit_note", "delete_note", "remove_note"))
    with pytest.raises(WorkbenchError, match="cannot be empty"):
        add_handling_note(store, exc.exception_id, "Tom Alvarez", "   ")
    events = [e for e in store.audit_events(exc.exception_id) if e.action == "exception_note_added"]
    assert len(events) == 2


def test_view_counts_foot_and_filters_apply():
    store = _store_with_exceptions()
    view = workbench_view(store, date(2026, 9, 10))
    assert view["total_open"] == 2
    assert sum(v["count"] for v in view["counts_by_code"].values()) == view["total_open"]
    assert sum(D(v["amount"]) for v in view["counts_by_code"].values()) == D(view["total_amount"])
    assert {r["code"] for r in view["rows"]} == {"UNIDENTIFIED", "PARTIAL"}
    # Oldest first: the Sep 2 line leads.
    assert view["rows"][0]["first_seen"] <= view["rows"][1]["first_seen"]
    assert view["rows"][0]["age_days"] == 8

    only_unidentified = workbench_view(store, date(2026, 9, 10), code="UNIDENTIFIED")
    assert only_unidentified["filtered_count"] == 1
    assert only_unidentified["total_open"] == 2  # counts still describe the whole queue

    exc = next(e for e in store.exceptions(status="open") if e.code.value == "PARTIAL")
    assign_exception(store, exc.exception_id, "Priya Nair", actor="cash-team")
    unassigned = workbench_view(store, date(2026, 9, 10), unassigned=True)
    assert [r["code"] for r in unassigned["rows"]] == ["UNIDENTIFIED"]
    mine = workbench_view(store, date(2026, 9, 10), assignee="Priya Nair")
    assert [r["code"] for r in mine["rows"]] == ["PARTIAL"]
    assert mine["rows"][0]["assignee"] == "Priya Nair"
