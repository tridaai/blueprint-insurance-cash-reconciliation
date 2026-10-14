"""Regressions found in hands-on POC testing: duplicate cash while
approval lags, name games at the gate, re-seeding a book in use, and
a model reading that copies the document's thousands separators."""

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import GateError, approve_pack, reject_pack
from cash_recon.llm import LLMReader, OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.rematch import rematch_suspense
from cash_recon.store import InMemoryStore
from tests.conftest import ASSEMBLER, CONTROLLER

D = Decimal


def _run(store, objects, day, reader=None):
    return run_day(store, objects, reader or OfflineReader(), PolicyConfig(), day, actor=ASSEMBLER)


def _codes(result, line_id):
    outcome = next(o for o in result["outcomes"] if o["bank_line_id"] == line_id)
    return {e["code"].value if hasattr(e["code"], "value") else e["code"]
            for e in outcome["exceptions"]}


# -- duplicate cash while approval lags ----------------------------------------

def test_duplicate_is_caught_while_the_first_copy_waits_at_the_gate(world):
    _ds, store, objects = world
    _run(store, objects, date(2026, 9, 7))  # BL-005, Saffron 6,120.00 — left undecided
    result = _run(store, objects, date(2026, 9, 10))  # BL-015, the same 6,120.00 again
    assert "DUPLICATE_PAYMENT" in _codes(result, "BL-015")
    pack = store.pack(result["pack_id"])
    assert not [e for e in pack.entries if e.allocation and e.allocation.bank_line_id == "BL-015"]


def test_gate_refuses_cash_an_installment_can_no_longer_take(world):
    _ds, store, objects = world
    result = _run(store, objects, date(2026, 9, 2))  # BL-001 -> NSP-1001-I1, 4,800.00
    rec = store.receivable("NSP-1001")
    rec.installments[0].amount_applied = D("4800.00")  # settled elsewhere since assembly
    store.upsert_receivable(rec)
    with pytest.raises(GateError, match="can take only 0.00"):
        approve_pack(store, result["pack_id"], CONTROLLER, PolicyConfig())
    assert store.pack(result["pack_id"]).status.value == "proposed"
    assert store.receivable("NSP-1002").installments[0].amount_applied == D("0.00")


# -- names at the gate ------------------------------------------------------------

def test_self_approval_is_refused_whatever_the_case_and_spacing(world):
    _ds, store, objects = world
    result = _run(store, objects, date(2026, 9, 2))
    with pytest.raises(GateError, match="self-approval"):
        approve_pack(store, result["pack_id"], "  CASH-team ", PolicyConfig())
    with pytest.raises(GateError, match="assembler"):
        reject_pack(store, result["pack_id"], "Cash-Team", "no")


def test_blank_approver_is_refused_and_names_are_recorded_trimmed(world):
    _ds, store, objects = world
    result = _run(store, objects, date(2026, 9, 2))
    with pytest.raises(GateError, match="name the approver"):
        approve_pack(store, result["pack_id"], "   ", PolicyConfig())
    pack = approve_pack(store, result["pack_id"], f"  {CONTROLLER}  ", PolicyConfig())
    assert pack.approvals == [CONTROLLER]


# -- seeding a book in use -----------------------------------------------------

def test_seed_is_refused_once_the_book_holds_bank_lines():
    service = Service(InMemoryStore(), InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    http = TestClient(create_app(service))
    assert http.post("/api/seed").status_code == 200
    again = http.post("/api/seed")
    assert again.status_code == 409 and "seed refused" in again.json()["detail"]


# -- model readings with document formatting -------------------------------------

DUNMORE = (
    "Policy NSP-1005 — Kestrel Warehousing Inc — 19,425.00\n"
    "Policy NSP-1099 — Kestrel Aviation Ltd — 2,500.00\n"
    "Total remitted: 21,925.00"
)


def test_reader_accepts_amounts_written_with_separators_and_symbols():
    def chat(messages):
        return json.dumps({"lines": [
            {"policy_number": "NSP-1005", "amount": "19,425.00",
             "citation": "Policy NSP-1005 — Kestrel Warehousing Inc — 19,425.00"},
            {"policy_number": "NSP-1099", "amount": "$2,500.00",
             "citation": "Policy NSP-1099 — Kestrel Aviation Ltd — 2,500.00"},
        ]})

    lines = LLMReader(chat, "stub").read_remittance(DUNMORE)
    assert [(l.policy_number, l.amount) for l in lines] == [
        ("NSP-1005", D("19425.00")), ("NSP-1099", D("2500.00"))]


def test_an_unreadable_advice_holds_its_wire_and_the_day_completes(world):
    _ds, store, objects = world
    broken = LLMReader(lambda messages: "I cannot help with that.", "broken")
    result = _run(store, objects, date(2026, 9, 9), reader=broken)  # BL-012, Dunmore's wire
    assert _codes(result, "BL-012") == {"REMITTANCE_UNREADABLE"}
    outcome = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-012")
    assert outcome["suspense_amount"] == D("21925.00") and not outcome["allocations"]
    assert result["report"]["footing_ok"] is True
    assert any("could not be read" in n for n in result["document_notes"])

    # The re-match pass reads the advice again with a working reader.
    summary = rematch_suspense(store, OfflineReader(), PolicyConfig(), CONTROLLER, date(2026, 9, 10))
    rem = next(r for r in store.remittances() if r.remittance_id == "REM-DUNMORE-01")
    assert rem.read_failed is False and len(rem.lines) == 2
    assert summary["pack_id"] is not None


# -- two people deciding the same pack at the same moment ----------------------

def _race(n, fn):
    import threading

    out, barrier = [None] * n, threading.Barrier(n)

    def go(i):
        barrier.wait()
        try:
            out[i] = ("ok", fn(i))
        except GateError as exc:
            out[i] = ("refused", str(exc))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [o[0] for o in out]


def _partial_day(store_factory):
    from cash_recon.dataset import build_dataset
    from cash_recon.pipeline import ingest_dataset

    store, objects = store_factory(), InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    result = _run(store, objects, date(2026, 9, 4))  # BL-006: 7,000.00 against NSP-1037's 7,260.00
    return store, result["pack_id"]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_simultaneous_approvals_apply_the_cash_once(kind, tmp_path):
    from cash_recon.store import SqliteStore

    factory = InMemoryStore if kind == "memory" else (lambda: SqliteStore(tmp_path / "race.db"))
    store, pack_id = _partial_day(factory)
    outcomes = _race(5, lambda i: approve_pack(store, pack_id, CONTROLLER, PolicyConfig()))
    assert sorted(outcomes) == ["ok", "refused", "refused", "refused", "refused"]
    inst = store.receivable("NSP-1037").installments[0]
    assert inst.amount_applied == D("7000.00")  # not 7,260.00: the 260.00 still owed stands
    approvals = [e for e in store.audit_events() if e.action == "pack_approved" and e.subject == pack_id] \
        if hasattr(store, "audit_events") else None
    if approvals is not None:
        assert len(approvals) == 1


def test_approve_and_reject_at_the_same_moment_decide_once(world):
    _ds, store, objects = world
    pack_id = _run(store, objects, date(2026, 9, 4))["pack_id"]
    outcomes = _race(2, lambda i: [
        lambda: approve_pack(store, pack_id, CONTROLLER, PolicyConfig()),
        lambda: reject_pack(store, pack_id, "Mei-Ling Chen", "hold"),
    ][i]())
    assert sorted(outcomes) == ["ok", "refused"]
    status = store.pack(pack_id).status.value
    applied = store.receivable("NSP-1037").installments[0].amount_applied
    assert (status, applied) in (("approved", D("7000.00")), ("rejected", D("0.00")))


# -- bank files read exactly, or refused --------------------------------------

def _books():
    from cash_recon.dataset import build_dataset

    store = InMemoryStore()
    for b in build_dataset().brokers:
        store.upsert_broker(b)
    return store


@pytest.mark.parametrize("cell", ['"4.800,00"', '"4,800.00"', '"4800,00"', "4800.00", '"€4.800,00"'])
def test_amounts_read_whatever_the_separator_convention(cell):
    from cash_recon.statement import import_statement

    store = _books()
    import_statement(store, f"Date,Description,Reference,Debit,Credit\n2026-09-23,Marigold — NSP-1001,T1,,{cell}\n", "f.csv")
    (line,) = store.bank_lines()
    assert line.amount == D("4800.00")


@pytest.mark.parametrize("cell", ['"1,23,456.00"', '"4.80.0"', '"12,3456"'])
def test_amounts_that_cannot_be_read_with_certainty_are_refused(cell):
    from cash_recon.statement import StatementImportError, import_statement

    store = _books()
    with pytest.raises(StatementImportError, match="does not parse as money"):
        import_statement(store, f"Date,Description,Reference,Debit,Credit\n2026-09-23,X,T1,,{cell}\n", "f.csv")
    assert store.bank_lines() == []


def test_identical_rows_in_one_file_are_two_payments_and_reimport_adds_none():
    from cash_recon.statement import import_statement

    store = _books()
    text = ("Date,Description,Reference,Debit,Credit\n"
            "2026-09-23,Cash deposit,,,500.00\n2026-09-23,Cash deposit,,,500.00\n")
    first = import_statement(store, text, "f.csv")
    assert first["imported"] == 2 and len(store.bank_lines()) == 2
    again = import_statement(store, text, "f.csv")
    assert again["imported"] == 0 and again["skipped_duplicates"] == 2 and len(store.bank_lines()) == 2


def test_numeric_dates_take_one_order_for_the_whole_file():
    from cash_recon.statement import StatementImportError, import_statement

    head = "Date,Description,Reference,Debit,Credit\n"
    store = _books()  # 09/23 proves month-first, so 09/10 is 10 September
    report = import_statement(store, head + "09/23/2026,A,T1,,1.00\n09/10/2026,B,T2,,2.00\n", "us.csv")
    assert report["date_order"] == "MDY"
    assert sorted(l.value_date for l in store.bank_lines()) == [date(2026, 9, 10), date(2026, 9, 23)]

    store = _books()
    with pytest.raises(StatementImportError, match="reads either way"):
        import_statement(store, head + "09/10/2026,B,T2,,2.00\n", "ambiguous.csv")
    assert store.bank_lines() == []
    import_statement(store, head + "09/10/2026,B,T2,,2.00\n", "ambiguous.csv", date_order="MDY")
    assert store.bank_lines()[0].value_date == date(2026, 9, 10)

    store = _books()
    with pytest.raises(StatementImportError, match="mixes date orders"):
        import_statement(store, head + "23/09/2026,A,T1,,1.00\n09/23/2026,B,T2,,2.00\n", "mixed.csv")
    assert store.bank_lines() == []


# -- the model provider down ---------------------------------------------------

def _unreachable(messages):
    import urllib.error

    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def test_a_provider_outage_holds_the_wire_and_the_day_completes(world):
    _ds, store, objects = world
    down = LLMReader(_unreachable, "down")
    result = _run(store, objects, date(2026, 9, 7), reader=down)  # BL-008: Alder's advised wire
    assert _codes(result, "BL-008") == {"REMITTANCE_UNREADABLE"}
    assert result["report"]["footing_ok"] is True
    assert any("unreachable" in n for n in result["document_notes"])


def test_a_provider_outage_never_invents_a_lump_sum_or_loses_the_query_note(world):
    _ds, store, objects = world
    down = LLMReader(_unreachable, "down")
    note_day = _run(store, objects, date(2026, 9, 3), reader=down)  # BL-004: netting mismatch, note drafted
    assert note_day["report"]["footing_ok"] is True
    lump = _run(store, objects, date(2026, 9, 11), reader=down)  # BL-017: lump sum, no advice
    outcome = next(o for o in lump["outcomes"] if o["bank_line_id"] == "BL-017")
    assert not outcome["allocations"] and outcome["suspense_amount"] == D("22072.16")
