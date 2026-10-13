"""The per-line audit export: every cent and every act for one line.

Two worlds, built the way the desk works them:

- a rich line — a remittance read, edited at the gate, approved,
  returned by the bank, and re-sent — whose export must show the
  whole chain and foot;
- a plain T1 line, whose export is minimal but complete.

Plus the export's own disciplines: byte-identical regeneration,
and proof that building it changes nothing in the books.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from cash_recon import gate
from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig
from cash_recon.line_export import LineExportError, line_export, render_line_export_text
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
from cash_recon.rematch import rematch_suspense
from cash_recon.returns import record_return
from cash_recon.store import InMemoryStore, dump

D = Decimal
CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"
BROKER = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))


def _policy(number, premium, broker_id="BRK-TEST"):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


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


def rich_world():
    """BL-Q1: a remittance wire, edited at the gate, approved,
    returned, and re-sent as BL-Q2 under a fresh advice."""
    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.upsert_broker(BROKER)
    store.upsert_receivable(_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(_policy("NSP-2002", D("3000.00")))
    store.add_remittance(_advice("REM-Q1", "2026-09-05",
                                  [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))],
                                  D("4500.00")))
    store.add_bank_line(BankLine(bank_line_id="BL-Q1", value_date=date(2026, 9, 5),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    result = run_day(store, objects, OfflineReader(), policy,
                     date(2026, 9, 5), actor=ASSEMBLER)
    pack = store.pack(result["pack_id"])
    idx = {e.allocation.policy_number: i for i, e in enumerate(pack.entries)}
    gate.edit_pack_entries(store, result["pack_id"], CONTROLLER, [
        {"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")},
        {"entry_index": idx["NSP-2002"], "new_amount": D("2800.00")},
    ])
    gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
    record_return(store, "BL-Q1", "NSF", CONTROLLER, date(2026, 9, 7))

    store.add_remittance(_advice("REM-Q2", "2026-09-08",
                                  [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                   ("NSP-2002", "Beta Test Co", D("2700.00"))],
                                  D("4500.00")))
    store.add_bank_line(BankLine(bank_line_id="BL-Q2", value_date=date(2026, 9, 8),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    resent = run_day(store, objects, OfflineReader(), policy,
                     date(2026, 9, 8), actor=ASSEMBLER)
    gate.approve_pack(store, resent["pack_id"], CONTROLLER, policy)
    return store


def _snapshot(store) -> str:
    parts = []
    for rows in (
        store.brokers(), store.receivables(), store.bank_lines(),
        store.remittances(), store.exceptions(), store.suspense_items(),
        store.packs(), store.returns(), store.payables(),
        store.payout_packs(), store.audit_events(),
    ):
        parts.append([json.loads(dump(r)) for r in rows])
    parts.append([json.loads(dump(a)) for a in store.allocations()])
    return json.dumps(parts, sort_keys=True, default=str)


def test_rich_line_exports_completely_and_foots():
    store = rich_world()
    export = line_export(store, "BL-Q1")
    body = export["export"]

    # The ladder: one working, settled at T4 on the reader's reading.
    assert body["attempts_recorded"] is True
    assert len(body["attempts"]) == 1
    stages = body["attempts"][0]["stages"]
    assert [s["stage"] for s in stages] == ["T4_REMITTANCE"]
    assert stages[0]["result"] == "settled"

    # Allocations carry their basis, their proposer, and their edits.
    assert len(body["allocations"]) == 2
    for alloc in body["allocations"]:
        assert alloc["pack_id"] == "PACK-2026-09-05"
        assert alloc["pack_status"] == "approved"
        assert alloc["tier"] == "T4_REMITTANCE"
        assert alloc["proposal_kind"] == "remittance_read"
        assert alloc["basis"].startswith("net of commission")
        assert len(alloc["edits"]) == 1
        edit = alloc["edits"][0]
        assert edit["by"] == CONTROLLER
        assert edit["before"]["amount"] in ("1800.00", "2700.00")
        assert edit["after"]["amount"] in ("1700.00", "2800.00")

    # Gate history: assembler, approver, the flag the return raised.
    [history] = body["gate_history"]
    assert history["assembled_by"] == ASSEMBLER
    assert history["approvals"] == [CONTROLLER]
    assert history["flags"] == ["RETURN_AFFECTED"]
    assert len(history["edits"]) == 2

    # Return and re-send, linked both ways.
    assert body["return"]["return_id"] == "RET-BL-Q1"
    assert body["resend"]["resent_as"] == ["BL-Q2"]
    assert body["final_state"]["state"] == "returned"

    resend = line_export(store, "BL-Q2")["export"]
    assert resend["resend"]["resend_of"] == "BL-Q1"
    assert resend["resend"]["resend_of_return_id"] == "RET-BL-Q1"
    assert resend["footing"]["foots"] is True

    # The footing accounts for the returned line to the cent.
    footing = body["footing"]
    assert footing["allocations_posted"] == "4500.00"
    assert footing["allocations_reversed_by_return"] == "4500.00"
    assert footing["returned_amount"] == "4500.00"
    assert footing["accounted"] == "4500.00"
    assert footing["foots"] is True

    text = render_line_export_text(export)
    assert "PER-LINE AUDIT EXPORT — BL-Q1" in text
    assert "The line FOOTS" in text
    assert "RET-BL-Q1" in text and "BL-Q2" in text


def test_plain_t1_line_exports_minimally_but_completely():
    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.upsert_receivable(_policy("NSP-3001", D("1000.00"), broker_id=None))
    store.add_bank_line(BankLine(bank_line_id="BL-T1X", value_date=date(2026, 9, 3),
                                 amount=D("1000.00"), payer_name="Insured NSP-3001",
                                 reference="NSP-3001", narrative=""))
    result = run_day(store, objects, OfflineReader(), policy,
                     date(2026, 9, 3), actor=ASSEMBLER)
    gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)

    body = line_export(store, "BL-T1X")["export"]
    stages = [s["stage"] for s in body["attempts"][0]["stages"]]
    assert stages == ["DUPLICATE_CHECK", "T1_EXACT"]
    [alloc] = body["allocations"]
    assert alloc["basis"] == "gross (billed amount)"
    assert alloc["tier"] == "T1_EXACT"
    assert alloc["edits"] == []
    assert body["exceptions"] == []
    assert body["return"] is None
    assert body["final_state"]["state"] == "applied"
    assert body["footing"]["foots"] is True
    assert body["footing"]["accounted"] == "1000.00"
    assert body["source"]["kind"] == "manual"


def test_export_is_byte_identical_and_read_only():
    store = rich_world()
    before = _snapshot(store)
    first = line_export(store, "BL-Q1")
    render_line_export_text(first)
    second = line_export(store, "BL-Q1")
    assert first["sha256"] == second["sha256"]
    assert first == second
    assert _snapshot(store) == before


def test_unrecorded_attempts_are_stated_not_invented():
    store = InMemoryStore()
    store.add_bank_line(BankLine(bank_line_id="BL-OLD", value_date=date(2026, 8, 1),
                                 amount=D("250.00"), payer_name="Someone",
                                 reference=None, narrative=""))
    export = line_export(store, "BL-OLD")
    body = export["export"]
    assert body["attempts_recorded"] is False
    assert body["attempts"] == []
    assert body["final_state"]["state"] == "received, not yet worked"
    text = render_line_export_text(export)
    assert "were not recorded for this line" in text


def test_rematch_attempts_and_ladder_appear_on_the_export():
    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.add_bank_line(BankLine(bank_line_id="BL-M1", value_date=date(2026, 9, 3),
                                 amount=D("800.00"), payer_name="Late Policy Co",
                                 reference="NSP-9001", narrative=""))
    run_day(store, objects, OfflineReader(), policy, date(2026, 9, 3), actor=ASSEMBLER)
    store.upsert_receivable(_policy("NSP-9001", D("800.00"), broker_id=None))
    rematch_suspense(store, OfflineReader(), policy, ASSEMBLER, date(2026, 9, 5))
    pack_id = "PACK-REMATCH-2026-09-05"
    gate.approve_pack(store, pack_id, CONTROLLER, policy)

    body = line_export(store, "BL-M1")["export"]
    # Two workings of the ladder: the day run's and the re-match's.
    assert len(body["attempts"]) == 2
    day_stages = [s["stage"] for s in body["attempts"][0]["stages"]]
    assert "REFERENCE_LOOKUP" in day_stages
    [attempt] = body["rematch_attempts"]
    assert attempt["suspense_id"] == "SUS-BL-M1"
    assert "matched at T1_EXACT" in attempt["detail"]
    [history] = [g for g in body["gate_history"] if g["pack_id"] == pack_id]
    assert history["kind"] == "rematch"
    assert body["footing"]["foots"] is True
    assert body["footing"]["allocations_posted"] == "800.00"
    [exc] = body["exceptions"]
    assert exc["code"] == "UNKNOWN_POLICY" and exc["status"] == "resolved"


def test_imported_line_traces_to_its_batch():
    from pathlib import Path

    from cash_recon.statement import import_statement

    store = InMemoryStore()
    text = (Path(__file__).parent.parent / "data" / "example-statement.csv").read_text()
    report = import_statement(store, text, "example-statement.csv")
    line_id = report["imported_ids"][0]
    body = line_export(store, line_id)["export"]
    assert body["source"]["kind"] == "import"
    assert body["source"]["import_batch"] == report["batch_id"]
    assert body["source"]["import"]["source_file"] == "example-statement.csv"


def test_export_api_and_unknown_line():
    store = rich_world()
    client = TestClient(create_app(Service(store, InMemoryObjectStore(),
                                            OfflineReader(), PolicyConfig())))
    ok = client.get("/api/bank-lines/BL-Q1/audit-export")
    assert ok.status_code == 200
    assert ok.json()["export"]["footing"]["foots"] is True
    text = client.get("/api/bank-lines/BL-Q1/audit-export/text")
    assert text.status_code == 200
    assert "PER-LINE AUDIT EXPORT — BL-Q1" in text.text
    assert client.get("/api/bank-lines/BL-NOPE/audit-export").status_code == 404
    with pytest.raises(LineExportError):
        line_export(store, "BL-NOPE")
