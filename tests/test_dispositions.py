"""Suspense disposition (round 14): the governed end of the
suspense lifecycle.

Pins:

- eligibility is code: open, wholly unapplied, and at or past
  the disposition age floor (default 90 days) — and refused, in
  words naming the blocker, for a too-young item, a partially
  applied item, an item with a standing undecided resolution
  docket, and an item whose line carries an open return chain;
- assembly is per item, with a kind and a written reason, and
  leaves the items OPEN — one standing docket per item;
- approval is dual control ALWAYS: no second approver, a second
  approver who is the first, a second approver who is the
  assembler, and the assembler as approver are each refused with
  the books unchanged;
- approval moves the item into its disposition state with the
  full chain on the record, closes the line's open exceptions,
  and the close pack takes the cash out of the suspense identity
  through disposed_from_suspense — per currency, split by kind;
- an escheat-review item is visible on its own list and is never
  income; a rejected pack leaves its items open and aging;
- the read models follow: suspense aging no longer counts the
  item, the day report names the decision day's dispositions,
  dwell ends at the disposition date, and the per-line audit
  export foots with the disposition named.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon import gate
from cash_recon.close_pack import close_pack_export
from cash_recon.config import Policy as PolicyConfig
from cash_recon.dispositions import (
    DispositionError,
    assemble_disposition_pack,
    disposition_candidates,
    disposition_ledger,
)
from cash_recon.gate import GateError
from cash_recon.line_export import line_export
from cash_recon.models import (
    BankLine,
    ExceptionCode,
    ExceptionRecord,
    Installment,
    PolicyStatus,
    PostingPack,
    Receivable,
    ResolutionInfo,
    SuspenseItem,
)
from cash_recon.report import day_report, suspense_aging
from cash_recon.returns import record_return
from cash_recon.store import InMemoryStore
from cash_recon.trends import match_quality

D = Decimal
ASSEMBLER = "cash-team"
FIRST = "Priya Nair"
SECOND = "Tomas Reyes"
RECEIVED = date(2026, 6, 15)      # 127 days before AS_OF: past the floor
AS_OF = date(2026, 10, 20)


def _park(store, line, reason="UNIDENTIFIED", exception=True):
    store.add_bank_line(line)
    store.save_suspense(SuspenseItem(
        suspense_id=f"SUS-{line.bank_line_id}", bank_line_id=line.bank_line_id,
        amount=line.amount, received_date=line.value_date, reason=reason,
        currency=line.currency))
    if exception:
        store.add_exception(ExceptionRecord(
            exception_id=f"EXC-{line.bank_line_id}", code=ExceptionCode.UNIDENTIFIED,
            bank_line_id=line.bank_line_id, policy_number=None, broker_id=None,
            amount=line.amount, currency=line.currency,
            detail="planted unidentified cash", first_seen=line.value_date))
    return f"SUS-{line.bank_line_id}"


def _aged_world(amount="1250.00", currency="USD"):
    store = InMemoryStore()
    sid = _park(store, BankLine(
        bank_line_id="BL-D1", value_date=RECEIVED, amount=D(amount),
        payer_name="Unknown Remitter", reference=None,
        narrative="premium payment", currency=currency))
    return store, sid


def _spec(sid, kind="WRITE_OFF_INCOME", reason="payer untraceable after three letters"):
    return {"suspense_id": sid, "kind": kind, "reason": reason}


# ---------------------------------------------------------------- eligibility


def test_aged_wholly_unapplied_item_is_eligible_and_stays_open():
    store, sid = _aged_world()
    view = disposition_candidates(store, AS_OF, PolicyConfig())
    row = view["items"][0]
    assert row["eligible"] is True and row["blockers"] == []
    assert row["age_days"] == 127 and view["min_age_days"] == 90

    pack = assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())
    assert pack.pack_id == "PACK-DISPOSE-2026-10-20"
    assert pack.disposition.items[0].kind == "WRITE_OFF_INCOME"
    # Assembly proposes; the item stands open until the gate decides.
    assert store.suspense_items()[0].status == "open"
    # And while the pack stands undecided, the item cannot be
    # proposed a second time.
    with pytest.raises(DispositionError, match="undecided disposition pack"):
        assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())


def test_too_young_item_is_refused_in_words():
    store = InMemoryStore()
    sid = _park(store, BankLine(
        bank_line_id="BL-D2", value_date=date(2026, 9, 25), amount=D("300.00"),
        payer_name="Unknown", reference=None, narrative="premium"))
    with pytest.raises(DispositionError, match="90-day disposition floor"):
        assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())
    assert store.packs() == []


def test_age_floor_is_a_policy_threshold(monkeypatch):
    store = InMemoryStore()
    sid = _park(store, BankLine(
        bank_line_id="BL-D2", value_date=date(2026, 9, 25), amount=D("300.00"),
        payer_name="Unknown", reference=None, narrative="premium"))
    pack = assemble_disposition_pack(
        store, [_spec(sid)], ASSEMBLER, AS_OF,
        PolicyConfig(suspense_disposition_min_age_days=25))
    assert pack.disposition is not None
    monkeypatch.setenv("SUSPENSE_DISPOSITION_MIN_AGE_DAYS", "45")
    assert PolicyConfig.from_env().suspense_disposition_min_age_days == 45


def _partly_applied_world():
    """A line of 1000: 600 applied under an approved pack, 400
    standing in suspense as the unapplied remainder."""
    store = InMemoryStore()
    store.upsert_receivable(Receivable(
        policy_number="NSP-8001", insured_name="Insured NSP-8001",
        broker_id=None, status=PolicyStatus.ACTIVE,
        installments=[Installment(
            installment_id="NSP-8001-I1", policy_number="NSP-8001",
            sequence=1, due_date=date(2026, 6, 1), amount_due=D("1000.00"))]))
    line = BankLine(bank_line_id="BL-P1", value_date=RECEIVED,
                    amount=D("1000.00"), payer_name="Direct Payer",
                    reference="NSP-8001", narrative="part payment")
    store.add_bank_line(line)
    from cash_recon.models import Allocation, PostingEntry
    pack = PostingPack(pack_id="PACK-2026-06-15", value_date=RECEIVED,
                       assembled_by=ASSEMBLER)
    pack.entries.append(PostingEntry(kind="allocation", allocation=Allocation(
        bank_line_id="BL-P1", policy_number="NSP-8001",
        installment_id="NSP-8001-I1", amount=D("600.00"), tier=None)))
    store.save_pack(pack)
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig())
    store.save_suspense(SuspenseItem(
        suspense_id="SUS-BL-P1", bank_line_id="BL-P1", amount=D("400.00"),
        received_date=RECEIVED, reason="PARTIAL"))
    return store, "SUS-BL-P1"


def test_partially_applied_item_is_refused():
    store, sid = _partly_applied_world()
    with pytest.raises(DispositionError, match="not wholly unapplied"):
        assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())
    assert store.suspense_items()[0].status == "open"


def test_open_return_chain_blocks_disposition():
    store, sid = _partly_applied_world()
    # The bank takes 200 back: the return unwinds suspense first,
    # so the item shrinks to 200 — and its line's story is plainly
    # unfinished, which is the blocker the refusal must name.
    record_return(store, "BL-P1", "NSF", FIRST, date(2026, 8, 1), amount=D("200.00"))
    assert store.suspense_items()[0].amount == D("200.00")
    with pytest.raises(DispositionError, match="open return chain"):
        assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())


def test_standing_resolution_docket_blocks_disposition():
    store, sid = _aged_world()
    store.save_pack(PostingPack(
        pack_id="PACK-RESOLVE-2026-10-19-SUS-BL-D1", value_date=date(2026, 10, 19),
        assembled_by=ASSEMBLER,
        resolution=ResolutionInfo(
            suspense_id=sid, bank_line_id="BL-D1", amount=D("1250.00"))))
    with pytest.raises(DispositionError, match="undecided resolution pack"):
        assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())


def test_assembly_validation():
    store, sid = _aged_world()
    policy = PolicyConfig()
    with pytest.raises(DispositionError, match="no reason"):
        assemble_disposition_pack(
            store, [_spec(sid, reason="  ")], ASSEMBLER, AS_OF, policy)
    with pytest.raises(DispositionError, match="unknown disposition kind"):
        assemble_disposition_pack(
            store, [_spec(sid, kind="DELETE")], ASSEMBLER, AS_OF, policy)
    with pytest.raises(DispositionError, match="proposed twice"):
        assemble_disposition_pack(
            store, [_spec(sid), _spec(sid)], ASSEMBLER, AS_OF, policy)
    with pytest.raises(DispositionError, match="no such suspense item"):
        assemble_disposition_pack(
            store, [_spec("SUS-NOPE")], ASSEMBLER, AS_OF, policy)
    with pytest.raises(DispositionError, match="named person"):
        assemble_disposition_pack(store, [_spec(sid)], " ", AS_OF, policy)
    assert store.packs() == []


# ------------------------------------------------------------------ the gate


def _standing_pack():
    store, sid = _aged_world()
    pack = assemble_disposition_pack(
        store, [_spec(sid)], ASSEMBLER, AS_OF, PolicyConfig())
    return store, sid, pack


def test_dual_control_is_always_required():
    store, sid, pack = _standing_pack()
    policy = PolicyConfig()
    item_before = store.suspense_items()[0].model_copy(deep=True)

    with pytest.raises(GateError, match="dual-control act"):
        gate.approve_pack(store, pack.pack_id, FIRST, policy)
    with pytest.raises(GateError, match="must differ"):
        gate.approve_pack(store, pack.pack_id, FIRST, policy, second_approver=FIRST)
    with pytest.raises(GateError, match="neither approver"):
        gate.approve_pack(store, pack.pack_id, FIRST, policy, second_approver=ASSEMBLER)
    with pytest.raises(GateError, match="self-approval refused"):
        gate.approve_pack(store, pack.pack_id, ASSEMBLER, policy, second_approver=SECOND)
    with pytest.raises(GateError, match="carries no alternates"):
        gate.approve_pack(store, pack.pack_id, FIRST, policy,
                          second_approver=SECOND, choice="C1")
    # Every refusal left the books exactly as they stood.
    assert store.pack(pack.pack_id).status.value == "proposed"
    assert store.suspense_items()[0] == item_before

    decided = gate.approve_pack(store, pack.pack_id, FIRST, policy,
                                second_approver=SECOND)
    assert decided.status.value == "approved"
    assert decided.approvals == [FIRST, SECOND]


def test_approval_moves_the_item_and_closes_its_exceptions():
    store, sid, pack = _standing_pack()
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                      second_approver=SECOND)
    item = store.suspense_items()[0]
    assert item.status == "dispositioned"
    assert item.disposition_kind == "WRITE_OFF_INCOME"
    assert item.dispositioned_on == AS_OF
    assert item.disposition_pack_id == pack.pack_id
    assert "untraceable" in item.disposition_reason
    assert store.exceptions()[0].status == "resolved"
    trail = [e for e in store.audit_events()
             if e.action == "suspense_disposition_decided"]
    assert len(trail) == 1 and FIRST in trail[0].detail and SECOND in trail[0].detail


def test_rejection_leaves_the_item_open_and_reproposable():
    store, sid, pack = _standing_pack()
    gate.reject_pack(store, pack.pack_id, FIRST, "keep working this one")
    item = store.suspense_items()[0]
    assert item.status == "open" and item.disposition_kind is None
    assert disposition_ledger(store)["items"] == []
    again = assemble_disposition_pack(store, [_spec(sid)], ASSEMBLER,
                                      date(2026, 10, 21), PolicyConfig())
    assert again.pack_id == "PACK-DISPOSE-2026-10-21"


def test_books_moved_since_assembly_refuses_the_pack_whole():
    store, sid, pack = _standing_pack()
    item = store.suspense_items()[0]
    item.status = "resolved"
    item.resolved_on = AS_OF
    store.save_suspense(item)
    with pytest.raises(GateError, match="no longer stands"):
        gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                          second_approver=SECOND)
    after = store.suspense_items()[0]
    assert after.status == "resolved" and after.disposition_kind is None
    assert store.pack(pack.pack_id).status.value == "proposed"


def test_disposition_pack_takes_no_edits_and_no_write_offs():
    store, sid, pack = _standing_pack()
    with pytest.raises(GateError, match="edited by nobody"):
        gate.edit_pack_entries(store, pack.pack_id, FIRST,
                               [{"entry_index": 0, "new_amount": "1.00"}])
    store.add_exception(ExceptionRecord(
        exception_id="EXC-PART", code=ExceptionCode.PARTIAL,
        bank_line_id="BL-D1", policy_number="NSP-8001", broker_id=None,
        amount=D("10.00"), detail="a shortfall", first_seen=RECEIVED))
    with pytest.raises(GateError, match="do not attach to a disposition pack"):
        gate.add_write_off(store, pack.pack_id, "EXC-PART", FIRST)


# ------------------------------------------------------------- books and views


def test_write_off_foots_the_close_through_the_disposed_term():
    store, sid, pack = _standing_pack()
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                      second_approver=SECOND)

    close = close_pack_export(store, 2026, 10)["pack"]
    roll = close["control_roll"]
    assert roll["opening_suspense"] == "1250.00"
    assert roll["disposed_from_suspense"] == "1250.00"
    assert roll["disposed_by_kind"] == {
        "WRITE_OFF_INCOME": "1250.00", "ESCHEAT_REVIEW": "0.00"}
    assert roll["closing_suspense"] == "0.00" and roll["footed"] is True
    assert "disposed from suspense" in roll["footing_note"]
    # September — before the disposition — still shows the item
    # standing open: the disposition is dated, not retroactive.
    september = close_pack_export(store, 2026, 9)["pack"]["control_roll"]
    assert september["closing_suspense"] == "1250.00"
    assert september["disposed_from_suspense"] == "0.00"

    # Aging no longer counts the item, anywhere.
    assert all(b["count"] == 0 for b in suspense_aging(store, date(2026, 11, 1)))
    assert all(
        b["count"] == 0 for b in close["suspense_aging_at_month_end"])

    # The decision day's report names the disposition.
    report = day_report(store, AS_OF, PolicyConfig())
    section = report["suspense_dispositions"]
    assert section["count"] == 1
    assert section["items"][0]["kind"] == "WRITE_OFF_INCOME"
    assert section["decided_totals_by_kind"] == {"WRITE_OFF_INCOME": "1250.00"}

    # Dwell treats the disposition as the item's resolution:
    # received in June, so June's row carries dwell 127 days.
    june = next(m for m in match_quality(store)["months"] if m["month"] == "2026-06")
    assert june["suspense_resolved_items"] == 1
    assert june["avg_suspense_dwell_days"] == "127.00"

    # The posting file carries the disposition for the GL hand-off.
    text = gate.posting_file(store.pack(pack.pack_id))
    assert "suspense_disposition" in text and "WRITE_OFF_INCOME" in text


def test_escheat_review_is_listed_and_is_not_income():
    store, sid = _aged_world(amount="640.00")
    pack = assemble_disposition_pack(
        store, [_spec(sid, kind="ESCHEAT_REVIEW",
                      reason="payer dissolved; funds may be owed to the estate")],
        ASSEMBLER, AS_OF, PolicyConfig())
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                      second_approver=SECOND)

    ledger = disposition_ledger(store)
    assert [r["suspense_id"] for r in ledger["escheat_review"]] == [sid]
    assert ledger["by_kind"]["ESCHEAT_REVIEW"]["totals_by_currency"] == {"USD": "640.00"}
    assert "WRITE_OFF_INCOME" not in ledger["by_kind"]

    roll = close_pack_export(store, 2026, 10)["pack"]["control_roll"]
    assert roll["disposed_by_kind"]["ESCHEAT_REVIEW"] == "640.00"
    assert roll["disposed_by_kind"]["WRITE_OFF_INCOME"] == "0.00"
    assert roll["footed"] is True


def test_disposition_rolls_per_currency():
    store, sid = _aged_world()
    eur_sid = _park(store, BankLine(
        bank_line_id="BL-D9", value_date=date(2026, 6, 20), amount=D("4500.00"),
        payer_name="Unknown", reference=None, narrative="premium",
        currency="EUR"))
    pack = assemble_disposition_pack(
        store, [_spec(sid), _spec(eur_sid, kind="ESCHEAT_REVIEW")],
        ASSEMBLER, AS_OF, PolicyConfig())
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                      second_approver=SECOND)
    roll = close_pack_export(store, 2026, 10)["pack"]["control_roll"]
    assert roll["disposed_from_suspense"] == "1250.00"  # headline is USD
    assert roll["by_currency"]["USD"]["disposed_from_suspense"] == "1250.00"
    assert roll["by_currency"]["EUR"]["disposed_from_suspense"] == "4500.00"
    assert roll["by_currency"]["EUR"]["disposed_by_kind"]["ESCHEAT_REVIEW"] == "4500.00"
    assert roll["footed"] is True


def test_line_export_foots_with_the_disposition_named():
    store, sid, pack = _standing_pack()
    gate.approve_pack(store, pack.pack_id, FIRST, PolicyConfig(),
                      second_approver=SECOND)
    export = line_export(store, "BL-D1")["export"]
    assert export["footing"]["foots"] is True
    assert export["footing"]["suspense_dispositioned"] == "1250.00"
    assert export["final_state"]["state"] == "dispositioned from suspense"
    assert export["suspense_disposition"]["kind"] == "WRITE_OFF_INCOME"
    assert export["suspense_disposition"]["approvals"] == [FIRST, SECOND]
    kinds = {g["pack_id"]: g["kind"] for g in export["gate_history"]}
    assert kinds[pack.pack_id] == "suspense_disposition"


# ------------------------------------------------------------------------ API


def test_disposition_flow_over_http():
    from fastapi.testclient import TestClient

    from cash_recon.api import Service, create_app
    from cash_recon.llm import OfflineReader
    from cash_recon.objects import InMemoryObjectStore

    store, sid = _aged_world()
    service = Service(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    client = TestClient(create_app(service))

    view = client.get("/api/suspense/dispositions?as_of=2026-10-20").json()
    assert view["items"][0]["eligible"] is True

    refused = client.post("/api/suspense/dispositions/assemble", json={
        "actor": ASSEMBLER, "as_of": "2026-10-20",
        "items": [{"suspense_id": sid, "kind": "WRITE_OFF_INCOME", "reason": ""}]})
    assert refused.status_code == 409 and "no reason" in refused.json()["detail"]

    pack = client.post("/api/suspense/dispositions/assemble", json={
        "actor": ASSEMBLER, "as_of": "2026-10-20",
        "items": [{"suspense_id": sid, "kind": "ESCHEAT_REVIEW",
                   "reason": "payer untraceable; owed to someone"}]}).json()
    assert pack["pack_id"] == "PACK-DISPOSE-2026-10-20"

    single = client.post(f"/api/packs/{pack['pack_id']}/approve",
                         json={"approver": FIRST})
    assert single.status_code == 409 and "dual-control" in single.json()["detail"]

    ok = client.post(f"/api/packs/{pack['pack_id']}/approve",
                     json={"approver": FIRST, "second_approver": SECOND})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    ledger = client.get("/api/suspense/disposition-ledger").json()
    assert [r["suspense_id"] for r in ledger["escheat_review"]] == [sid]
