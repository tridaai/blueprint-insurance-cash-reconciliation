"""Gate webhooks: a stub receiver, the four event kinds, the
failure path, the retry, and disabled-by-default.

The discipline under test: the webhook never decides anything.
A dead receiver cannot stop a gate act; the failure lands in the
audit trail as `webhook_failed` with the error class, and the
books carry on.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from cash_recon import gate
from cash_recon.api import Service, create_app
from cash_recon.cancellations import record_cancellation
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.models import (
    BankLine,
    Installment,
    PolicyStatus,
    Receivable,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.payouts import assemble_payout_pack, approve_payout_pack
from cash_recon.pipeline import run_day
from cash_recon.returns import record_return
from cash_recon.store import InMemoryStore

D = Decimal
CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"
ENV = "CASH_RECON_WEBHOOK_URL"


class Receiver:
    """A local stub endpoint recording every POST body; can be
    told to fail the first N requests with HTTP 500."""

    def __init__(self, fail_first: int = 0):
        self.bodies: list[dict] = []
        self.fail_first = fail_first
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(json.loads(self.rfile.read(length)))
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(500)
                else:
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _world():
    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.upsert_receivable(Receivable(
        policy_number="NSP-7001", insured_name="Webhook Test Co", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-7001-I1", policy_number="NSP-7001",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("1000.00"))]))
    store.add_bank_line(BankLine(bank_line_id="BL-W1", value_date=date(2026, 9, 3),
                                 amount=D("1000.00"), payer_name="Webhook Test Co",
                                 reference="NSP-7001", narrative=""))
    return policy, store, objects


def _failed_events(store):
    return [e for e in store.audit_events() if e.action == "webhook_failed"]


def test_all_four_event_kinds_arrive_with_their_totals(monkeypatch):
    policy, store, objects = _world()
    with Receiver() as receiver:
        monkeypatch.setenv(ENV, receiver.url)
        result = run_day(store, objects, OfflineReader(), policy,
                         date(2026, 9, 3), actor=ASSEMBLER)
        gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
        record_return(store, "BL-W1", "NSF", CONTROLLER, date(2026, 9, 5))

        # A second day gives the reject path its own pack.
        store.add_bank_line(BankLine(bank_line_id="BL-W2", value_date=date(2026, 9, 4),
                                     amount=D("1000.00"), payer_name="Webhook Test Co",
                                     reference="NSP-7001", narrative=""))
        second = run_day(store, objects, OfflineReader(), policy,
                         date(2026, 9, 4), actor=ASSEMBLER)
        gate.reject_pack(store, second["pack_id"], CONTROLLER, "not our reading of the wire")

    kinds = [b["kind"] for b in receiver.bodies]
    assert kinds == ["pack_assembled", "pack_approved", "return_recorded",
                     "pack_assembled", "pack_rejected"]
    assembled, approved, returned = receiver.bodies[0], receiver.bodies[1], receiver.bodies[2]
    assert assembled["id"] == "PACK-2026-09-03" and assembled["actor"] == ASSEMBLER
    assert assembled["pack_kind"] == "posting"
    assert assembled["totals"]["allocations"] == "1000.00"
    assert approved["actor"] == CONTROLLER
    assert approved["totals"] == {"allocations": "1000.00", "write_offs": "0.00",
                                  "payable_offsets": "0.00"}
    assert returned["id"] == "BL-W1" and returned["actor"] == CONTROLLER
    assert returned["totals"]["amount"] == "1000.00"
    assert returned["totals"]["reason"] == "NSF"
    # Link-free: no internal paths or file references in any event.
    for body in receiver.bodies:
        assert "/" not in body["id"] or body["id"].startswith("PACK-")
        assert ".py" not in json.dumps(body) and "workspace" not in json.dumps(body)
    assert _failed_events(store) == []


def test_payout_pack_events_carry_the_payout_kind(monkeypatch):
    # A cancelled policy's return premium (the pinned arithmetic of
    # tests/test_cancellations.py: 12,000 premium, 6,000 paid, 91
    # days into a 365-day term → 3,008.22 back) opens the payable.
    policy = PolicyConfig()
    store = InMemoryStore()
    paid = Installment(installment_id="NSP-6001-I1", policy_number="NSP-6001",
                       sequence=1, due_date=date(2026, 9, 1), amount_due=D("6000.00"),
                       amount_applied=D("6000.00"), status="applied")
    unpaid = Installment(installment_id="NSP-6001-I2", policy_number="NSP-6001",
                         sequence=2, due_date=date(2027, 3, 1), amount_due=D("6000.00"))
    store.upsert_receivable(Receivable(
        policy_number="NSP-6001", insured_name="Payout Test Co", broker_id=None,
        status=PolicyStatus.ACTIVE, installments=[paid, unpaid],
        term_start=date(2026, 9, 1), term_end=date(2027, 9, 1)))
    record_cancellation(store, "NSP-6001", date(2026, 12, 1), CONTROLLER,
                        date(2026, 12, 1))
    with Receiver() as receiver:
        monkeypatch.setenv(ENV, receiver.url)
        pack = assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6001"],
                                    ASSEMBLER)
        approve_payout_pack(store, pack.pack_id, CONTROLLER, policy,
                            second_approver="Sofia Marino")
    kinds = [(b["kind"], b["pack_kind"]) for b in receiver.bodies]
    assert kinds == [("pack_assembled", "payout"), ("pack_approved", "payout")]
    assert receiver.bodies[0]["totals"]["payouts"] == "3008.22"
    assert receiver.bodies[1]["totals"]["payouts"] == "3008.22"


def test_dead_receiver_never_blocks_the_gate_act(monkeypatch):
    policy, store, objects = _world()
    monkeypatch.setenv(ENV, "http://127.0.0.1:1/hook")  # nothing listens there
    result = run_day(store, objects, OfflineReader(), policy,
                     date(2026, 9, 3), actor=ASSEMBLER)
    pack = gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
    assert pack.status.value == "approved"  # the act stands
    failed = _failed_events(store)
    assert {e.subject for e in failed} == {result["pack_id"]}
    assert all("URLError" in e.detail for e in failed)
    assert all(e.actor == "webhook" for e in failed)


def test_one_retry_then_delivery_succeeds(monkeypatch):
    policy, store, objects = _world()
    with Receiver(fail_first=1) as receiver:
        monkeypatch.setenv(ENV, receiver.url)
        result = run_day(store, objects, OfflineReader(), policy,
                         date(2026, 9, 3), actor=ASSEMBLER)
    # The assembly event took two attempts (500, then 200) and no
    # failure was audited: the retry recovered it.
    assert len(receiver.bodies) == 2
    assert receiver.bodies[0]["kind"] == "pack_assembled"
    assert _failed_events(store) == []


def test_disabled_by_default_no_calls_no_errors(monkeypatch):
    policy, store, objects = _world()
    monkeypatch.delenv(ENV, raising=False)
    result = run_day(store, objects, OfflineReader(), policy,
                     date(2026, 9, 3), actor=ASSEMBLER)
    gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
    assert _failed_events(store) == []


def test_readiness_reports_the_webhook_state(monkeypatch):
    _policy, store, objects = _world()
    client = TestClient(create_app(Service(store, objects, OfflineReader(), PolicyConfig())))
    monkeypatch.delenv(ENV, raising=False)
    assert client.get("/readiness").json()["webhooks"] == "disabled"
    monkeypatch.setenv(ENV, "http://127.0.0.1:9/hook")
    assert client.get("/readiness").json()["webhooks"] == "configured"
