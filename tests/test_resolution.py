"""Resolution proposals for unidentified cash (round 12).

The governed second look at the hardest pile: a wire nobody can
attribute. Pins:

- the scan's candidate discipline: exact arithmetic or verbatim
  evidence or nothing; the whole book is in scope because the
  payer is unknown (and the known-payer book restriction is
  untouched — the matcher's lump-sum path still refuses
  cross-broker proposals);
- flagged candidates (cancelled / no-open-balance policies named
  by the text) stand on the record and can never be chosen;
- the model may rank and may add only on verbatim citation — an
  uncited addition is refused, and the scan's candidates are
  never removed; offline, the scan stands alone, labelled;
- the pack is a docket of alternates: the item stays OPEN while
  it stands proposed, approval must name the chosen candidate,
  choosing applies exactly that candidate (at the line-date
  rate for broker cash), the rest lapse to the record;
- rejection leaves the item open and the candidates on the
  rejected pack; a policy cannot be double-proposed under the
  same reading while another docket stands undecided;
- approval moves the money exactly once: the day report still
  foots, the close pack's roll counts the release once, and the
  proposal-quality report grades chosen / lapsed / rejected.
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
    SuspenseItem,
)
from cash_recon.report import day_report, proposal_quality
from cash_recon.resolution import (
    ResolutionError,
    assemble_resolution_pack,
    scan_candidates,
)
from cash_recon.store import InMemoryStore

D = Decimal
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"
DAY = date(2026, 10, 5)
AS_OF = date(2026, 10, 6)


def _policy(number, premium, broker_id=None, installments=None, insured=None,
            status=PolicyStatus.ACTIVE):
    amounts = installments or [premium]
    return Receivable(
        policy_number=number, insured_name=insured or f"Insured {number}",
        broker_id=broker_id, status=status,
        installments=[Installment(installment_id=f"{number}-I{i + 1}",
                                  policy_number=number, sequence=i + 1,
                                  due_date=date(2026, 10, 1), amount_due=a)
                      for i, a in enumerate(amounts)],
    )


def _park(store, line, reason="UNIDENTIFIED"):
    store.add_bank_line(line)
    store.save_suspense(SuspenseItem(
        suspense_id=f"SUS-{line.bank_line_id}", bank_line_id=line.bank_line_id,
        amount=line.amount, received_date=line.value_date, reason=reason))
    return f"SUS-{line.bank_line_id}"


def _two_broker_world(narrative="Birch & Vale remittance — premium"):
    store = InMemoryStore()
    store.upsert_broker(Broker(broker_id="BRK-A", name="Alder Gate Insurance Brokers",
                               commission_rate=D("0.10")))
    store.upsert_broker(Broker(broker_id="BRK-B", name="Birch & Vale Brokers",
                               commission_rate=D("0.20")))
    store.upsert_receivable(_policy("NSP-7010", D("10000.00"), "BRK-A"))
    store.upsert_receivable(_policy("NSP-7011", D("11250.00"), "BRK-B"))
    sid = _park(store, BankLine(
        bank_line_id="BL-U1", value_date=DAY, amount=D("9000.00"),
        payer_name="Interstate Payments LLC", reference=None, narrative=narrative))
    return store, sid


# --------------------------------------------------------------------- scan


def test_two_plausible_brokers_only_the_evidenced_one_carries_evidence():
    store, sid = _two_broker_world()
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    by_policy = {c["policy_number"]: c for c in scan["candidates"]}
    assert set(by_policy) == {"NSP-7010", "NSP-7011"}
    assert by_policy["NSP-7011"]["interpretation"] == "netting"
    assert by_policy["NSP-7011"]["evidence"] == "Birch & Vale"
    assert by_policy["NSP-7011"]["evidence_kind"] == "broker_name"
    # The wrong-broker reading is present — it foots — but bare.
    assert by_policy["NSP-7010"]["evidence"] == ""
    assert scan["heuristic"] is True and scan["model_ranked"] is False
    # The scan is on the item's trail, and decided nothing.
    assert any(e.action == "suspense_resolution_scan" and e.subject == sid
               for e in store.audit_events())
    assert store.suspense_items()[0].status == "open"


def test_narrative_names_the_policy_with_a_mangled_reference():
    store = InMemoryStore()
    store.upsert_receivable(_policy(
        "NSP-7001", D("4000.00"), None,
        installments=[D("2000.00"), D("2000.00")], insured="Kestrel Point Marina"))
    sid = _park(store, BankLine(
        bank_line_id="BL-U2", value_date=DAY, amount=D("3300.00"),
        payer_name="R G Calloway", reference=None,
        narrative="premium for policy NSP 7001 — balance follows"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert len(scan["candidates"]) == 1
    cand = scan["candidates"][0]
    assert cand["interpretation"] == "narrative"
    assert cand["evidence"] == "NSP 7001"
    assert [(a["installment_id"], a["amount"]) for a in cand["allocations"]] == [
        ("NSP-7001-I1", "2000.00"), ("NSP-7001-I2", "1300.00")]


def test_exact_and_run_candidates_carry_their_arithmetic():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-7002", D("3600.00"), None,
                                    installments=[D("1800.00"), D("1800.00")]))
    store.upsert_receivable(_policy("NSP-7003", D("3600.00"), None))
    sid = _park(store, BankLine(
        bank_line_id="BL-U3", value_date=DAY, amount=D("3600.00"),
        payer_name="Cash deposit — branch 441", reference=None, narrative="cash deposit"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    readings = {(c["policy_number"], c["interpretation"]) for c in scan["candidates"]}
    assert readings == {("NSP-7002", "installment_run"), ("NSP-7003", "exact")}
    for cand in scan["candidates"]:
        assert sum(D(a["amount"]) for a in cand["allocations"]) == D("3600.00")
        assert cand["arithmetic"]  # shown, not implied


def test_no_candidate_means_no_pack_and_the_item_stays_open():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-7004", D("5000.00"), None))
    sid = _park(store, BankLine(
        bank_line_id="BL-U4", value_date=DAY, amount=D("777.77"),
        payer_name="Unknown", reference=None, narrative="premium payment"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert scan["candidates"] == []
    with pytest.raises(ResolutionError, match="no defensible candidate"):
        assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert store.packs() == []
    assert store.suspense_items()[0].status == "open"


def test_scan_scope_refusals_are_in_words():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-7005", D("1000.00"), None))
    sid = _park(store, BankLine(
        bank_line_id="BL-U5", value_date=DAY, amount=D("1000.00"),
        payer_name="X", reference=None, narrative="premium"), reason="PARTIAL")
    with pytest.raises(ResolutionError, match="held as PARTIAL"):
        scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    with pytest.raises(ResolutionError, match="no such suspense item"):
        scan_candidates(store, OfflineReader(), "SUS-NOPE", ASSEMBLER, AS_OF)


def test_flagged_candidate_for_a_cancelled_policy_named_in_the_text():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-7006", D("2000.00"), None,
                                    status=PolicyStatus.CANCELLED))
    sid = _park(store, BankLine(
        bank_line_id="BL-U6", value_date=DAY, amount=D("2000.00"),
        payer_name="Y", reference=None, narrative="payment for NSP-7006 thanks"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert len(scan["candidates"]) == 1
    cand = scan["candidates"][0]
    assert cand["flags"] == ["CANCELLED_POLICY"] and cand["allocations"] == []
    # A flagged-only scan assembles nothing.
    with pytest.raises(ResolutionError, match="no defensible candidate"):
        assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)


# -------------------------------------------------------------------- model


class _StubReader:
    """A stand-in connected reader whose replies go through the
    REAL LLMReader validation (verbatim-evidence discipline
    included): it ranks evidenced candidates first, and offers
    whatever additions it was built with."""

    name = "stub-model"

    def __init__(self, added=None):
        import json as _json

        from cash_recon.llm import LLMReader

        self._json = _json
        self._added = added or []
        self._reader = LLMReader.__new__(LLMReader)
        self._reader._ask = lambda system, user: self._reply

    def rank_resolution_candidates(self, amount, payer, line_text, candidates):
        from cash_recon.llm import LLMReader

        ranked = [c["candidate_id"] for c in candidates if c["evidence"]]
        rest = [c["candidate_id"] for c in candidates if not c["evidence"]]
        self._reply = self._json.dumps({
            "ranked": ranked + rest,
            "explanations": ({ranked[0]: "the narrative names this desk"}
                             if ranked else {}),
            "added": self._added,
        })
        return LLMReader.rank_resolution_candidates(
            self._reader, amount, payer, line_text, candidates)


def test_model_ranks_but_never_removes_and_uncited_additions_are_refused():
    store, sid = _two_broker_world()
    store.upsert_receivable(_policy("NSP-7099", D("9000.00"), None))
    reader = _StubReader(added=[
        # An invented quote: not in the line's text, refused.
        {"policy_number": "NSP-7099", "evidence": "policy NSP 7099"},
        # A duplicate of a standing candidate, also refused.
        {"policy_number": "NSP-7010", "evidence": "Birch & Vale"},
    ])
    scan = scan_candidates(store, reader, sid, ASSEMBLER, AS_OF)
    policies = {c["policy_number"] for c in scan["candidates"]}
    # Both scan candidates survive ranking; nothing was added.
    assert policies == {"NSP-7010", "NSP-7011", "NSP-7099"}
    assert all(c["proposed_by"] == "resolution_scan" for c in scan["candidates"])
    evidenced = next(c for c in scan["candidates"] if c["policy_number"] == "NSP-7011")
    assert evidenced["model_rank"] == 1
    assert "names this desk" in evidenced["model_explanation"]
    assert scan["model_ranked"] is True
    assert scan["scan_backend"] == "resolution_scan+model:stub-model"


def test_model_added_candidate_on_verbatim_evidence_the_scan_could_not_read():
    store, sid = _two_broker_world(
        narrative="premium for the marina account, policy #7099 — thanks")
    # The scan cannot read "#7099" as a reference, and the amount
    # fits none of this policy's arithmetic (5000 + 5000 != 9000):
    # only the model's pointer, on verbatim text, can propose it.
    store.upsert_receivable(_policy("NSP-7099", D("10000.00"), None,
                                    installments=[D("5000.00"), D("5000.00")]))
    reader = _StubReader(added=[{"policy_number": "NSP-7099", "evidence": "#7099"}])
    scan = scan_candidates(store, reader, sid, ASSEMBLER, AS_OF)
    added = [c for c in scan["candidates"] if c["proposed_by"] == "stub-model"]
    assert len(added) == 1
    cand = added[0]
    assert cand["policy_number"] == "NSP-7099"
    assert cand["interpretation"] == "narrative" and cand["evidence"] == "#7099"
    assert [(a["installment_id"], a["amount"]) for a in cand["allocations"]] == [
        ("NSP-7099-I1", "5000.00"), ("NSP-7099-I2", "4000.00")]
    # And it is decidable like any other candidate.
    pack = assemble_resolution_pack(store, reader, sid, ASSEMBLER, AS_OF)
    gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                      choice=cand["candidate_id"])
    assert store.receivable("NSP-7099").open_balance == D("1000.00")


# --------------------------------------------------------------------- gate


def test_approval_must_name_the_choice_and_applies_exactly_that_one():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert pack.pack_id.startswith("PACK-RESOLVE-")
    assert pack.entries == []  # a docket of alternates, not entries
    # The item stands open while the docket stands proposed.
    assert store.suspense_items()[0].status == "open"
    with pytest.raises(gate.GateError, match="must name the chosen candidate"):
        gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig())
    with pytest.raises(gate.GateError, match="self-approval refused"):
        gate.approve_pack(store, pack.pack_id, ASSEMBLER, PolicyConfig(), choice="C1")
    chosen = next(c for c in pack.resolution.candidates
                  if c.policy_number == "NSP-7011")
    gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                      choice=chosen.candidate_id)
    # The evidenced broker's policy settled in full at its own
    # rate; the other reading was never touched.
    assert store.receivable("NSP-7011").open_balance == D("0.00")
    assert store.receivable("NSP-7010").open_balance == D("10000.00")
    item = store.suspense_items()[0]
    assert item.status == "resolved"
    approved = store.pack(pack.pack_id)
    assert approved.resolution.chosen_candidate == chosen.candidate_id
    assert sum(e.allocation.amount for e in approved.entries if e.allocation) == D("9000.00")
    kinds = {(e.proposal_kind, e.proposed_by) for e in approved.entries}
    assert kinds == {("resolution", "resolution_scan")}
    # The day still foots, and the close roll counts the release once.
    assert day_report(store, DAY, PolicyConfig())["footing_ok"] is True
    body = close_pack_export(store, 2026, 10)["pack"]
    assert body["control_roll"]["footed"] is True
    assert body["control_roll"]["applied_from_suspense"] == "9000.00"


def test_flagged_and_unknown_choices_are_refused():
    store = InMemoryStore()
    store.upsert_receivable(_policy("NSP-7007", D("2000.00"), None,
                                    status=PolicyStatus.CANCELLED))
    store.upsert_receivable(_policy("NSP-7008", D("2000.00"), None))
    sid = _park(store, BankLine(
        bank_line_id="BL-U7", value_date=DAY, amount=D("2000.00"),
        payer_name="Z", reference=None,
        narrative="for NSP-7007 (their old policy) — premium"))
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    flagged = next(c for c in pack.resolution.candidates if c.flags)
    with pytest.raises(gate.GateError, match="cannot choose it"):
        gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                          choice=flagged.candidate_id)
    with pytest.raises(gate.GateError, match="carries no candidate"):
        gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(), choice="C99")
    assert store.pack(pack.pack_id).status.value == "proposed"


def test_a_choice_on_an_ordinary_pack_is_refused():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    from cash_recon.models import PostingPack

    ordinary = PostingPack(pack_id="PACK-2026-10-06", value_date=AS_OF,
                           assembled_by=ASSEMBLER)
    store.save_pack(ordinary)
    with pytest.raises(gate.GateError, match="carries no alternates"):
        gate.approve_pack(store, ordinary.pack_id, CONTROLLER, PolicyConfig(), choice="C1")


def test_rejection_leaves_the_item_open_and_the_candidates_on_record():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    gate.reject_pack(store, pack.pack_id, CONTROLLER, "not the desk's money")
    assert store.suspense_items()[0].status == "open"
    rejected = store.pack(pack.pack_id)
    assert len(rejected.resolution.candidates) == 2
    assert rejected.resolution.chosen_candidate is None
    assert any(e.action == "suspense_resolution_decided" and e.subject == sid
               for e in store.audit_events())
    quality = proposal_quality(store)
    assert quality["by_kind"]["resolution"]["rejected"] == 2
    # And the item can be worked again: a fresh docket assembles.
    again = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    assert again.pack_id != pack.pack_id


def test_double_proposal_of_one_reading_is_refused_naming_the_conflict():
    store, sid = _two_broker_world()
    _park(store, BankLine(
        bank_line_id="BL-U8", value_date=DAY, amount=D("9000.00"),
        payer_name="Interstate Payments LLC", reference=None,
        narrative="premium received, thank you"))
    first = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    with pytest.raises(ResolutionError, match="double-propose NSP-7010"):
        assemble_resolution_pack(store, OfflineReader(), "SUS-BL-U8", ASSEMBLER, AS_OF)
    # Deciding the first docket frees the reading.
    gate.reject_pack(store, first.pack_id, CONTROLLER, "worked the other wire first")
    second = assemble_resolution_pack(store, OfflineReader(), "SUS-BL-U8", ASSEMBLER, AS_OF)
    assert second.resolution.suspense_id == "SUS-BL-U8"


def test_second_docket_for_the_same_item_is_refused_while_one_stands():
    store, sid = _two_broker_world()
    assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    with pytest.raises(ResolutionError, match="already stands proposed"):
        assemble_resolution_pack(store, OfflineReader(), sid, "someone-else", AS_OF)


def test_books_moved_since_the_scan_refuses_the_approval_whole():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    chosen = next(c for c in pack.resolution.candidates
                  if c.policy_number == "NSP-7011")
    # Another act settles most of the installment meanwhile.
    rec = store.receivable("NSP-7011")
    rec.installments[0].amount_applied = D("200.00")
    store.upsert_receivable(rec)
    with pytest.raises(gate.GateError, match="no longer fits the books"):
        gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                          choice=chosen.candidate_id)
    assert store.pack(pack.pack_id).status.value == "proposed"
    assert store.suspense_items()[0].status == "open"


def test_resolution_pack_entries_and_write_offs_are_not_editable():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    with pytest.raises(gate.GateError, match="edited by nobody"):
        gate.edit_pack_entries(store, pack.pack_id, CONTROLLER,
                               [{"entry_index": 0, "new_amount": D("1.00")}])
    from cash_recon.models import ExceptionCode, ExceptionRecord

    exc = ExceptionRecord(exception_id="EXC-T1", code=ExceptionCode.PARTIAL,
                          bank_line_id="BL-U1", policy_number="NSP-7010",
                          broker_id=None, amount=D("10.00"),
                          detail="seeded", first_seen=DAY)
    store.add_exception(exc)
    with pytest.raises(gate.GateError, match="write-offs do not attach"):
        gate.add_write_off(store, pack.pack_id, "EXC-T1", CONTROLLER)


def test_quality_grades_chosen_and_lapsed():
    store, sid = _two_broker_world()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, AS_OF)
    chosen = next(c for c in pack.resolution.candidates
                  if c.policy_number == "NSP-7011")
    gate.approve_pack(store, pack.pack_id, CONTROLLER, PolicyConfig(),
                      choice=chosen.candidate_id)
    quality = proposal_quality(store)
    slot = quality["by_kind"]["resolution"]
    assert slot["proposals"] == 2 and slot["chosen"] == 1 and slot["lapsed"] == 1
    outcomes = {d["policy_number"]: d["outcome"] for d in quality["proposals"]}
    assert outcomes == {"NSP-7011": "CHOSEN", "NSP-7010": "LAPSED"}
    assert quality["totals"]["chosen"] == 1 and quality["totals"]["lapsed"] == 1


# ------------------------------------------------------------------------ api


def test_resolution_flow_over_http():
    from fastapi.testclient import TestClient

    from cash_recon.api import Service, create_app
    from cash_recon.objects import InMemoryObjectStore

    store, sid = _two_broker_world()
    service = Service(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    client = TestClient(create_app(service))

    scan = client.post(f"/api/suspense/{sid}/scan",
                       json={"actor": ASSEMBLER, "as_of": "2026-10-06"})
    assert scan.status_code == 200
    assert len(scan.json()["candidates"]) == 2

    assembled = client.post(f"/api/suspense/{sid}/resolution-pack",
                            json={"actor": ASSEMBLER, "as_of": "2026-10-06"})
    assert assembled.status_code == 200
    pack = assembled.json()
    assert pack["pack_id"].startswith("PACK-RESOLVE-")
    assert pack["resolution"]["suspense_id"] == sid
    assert pack["entries"] == []

    no_choice = client.post(f"/api/packs/{pack['pack_id']}/approve",
                            json={"approver": CONTROLLER})
    assert no_choice.status_code == 409
    assert "must name the chosen candidate" in no_choice.json()["detail"]

    chosen = next(c for c in pack["resolution"]["candidates"]
                  if c["policy_number"] == "NSP-7011")
    ok = client.post(f"/api/packs/{pack['pack_id']}/approve",
                     json={"approver": CONTROLLER, "choice": chosen["candidate_id"]})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert ok.json()["resolution"]["chosen_candidate"] == chosen["candidate_id"]
    assert store.receivable("NSP-7011").open_balance == D("0.00")

    refused = client.post("/api/suspense/SUS-NOPE/scan", json={"actor": ASSEMBLER})
    assert refused.status_code == 409
    assert "no such suspense item" in refused.json()["detail"]
