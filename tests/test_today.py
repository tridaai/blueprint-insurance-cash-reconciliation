"""The Today view: a scripted morning, step by step. Each step's
state must flip exactly when the underlying act happens — the
checklist holds no state of its own."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.cancellations import record_cancellation
from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import approve_pack
from cash_recon.llm import OfflineReader
from cash_recon.models import BankLine, Installment, PolicyStatus, Receivable
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.rematch import rematch_suspense
from cash_recon.store import InMemoryStore
from cash_recon.today import today_view

D = Decimal
DAY = date(2026, 9, 2)
POLICY = PolicyConfig()


def _states(store):
    view = today_view(store, DAY, POLICY)
    return view, {s["key"]: s["state"] for s in view["steps"]}


def _world():
    store = InMemoryStore()
    store.upsert_receivable(Receivable(
        policy_number="NSP-9001", insured_name="Insured NSP-9001", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-9001-I1", policy_number="NSP-9001",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("1000.00"))]))
    store.upsert_receivable(Receivable(
        policy_number="NSP-9002", insured_name="Insured NSP-9002", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-9002-I1", policy_number="NSP-9002",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("1200.00"),
                                   amount_applied=D("1200.00"), status="applied")],
        term_start=date(2026, 9, 1), term_end=date(2027, 9, 1)))
    return store


def test_the_morning_flips_step_by_step():
    store = _world()
    view, states = _states(store)
    assert [s["key"] for s in view["steps"]] == [
        "statement", "day_run", "exceptions", "gate", "rematch", "returns"]
    assert states == {"statement": "pending", "day_run": "pending",
                      "exceptions": "done", "gate": "done",
                      "rematch": "pending", "returns": "info"}
    assert view["pending"] == 3 and view["ready"] is False

    store.add_bank_line(BankLine(bank_line_id="BL-TD1", value_date=DAY, amount=D("500.00"),
                                 payer_name="A Stranger", reference=None, narrative=""))
    _view, states = _states(store)
    assert states["statement"] == "done" and states["day_run"] == "pending"

    result = run_day(store, InMemoryObjectStore(), OfflineReader(), POLICY, DAY, actor="cash-team")
    view, states = _states(store)
    assert states["day_run"] == "done"
    # The stranger's wire exceptioned: one open, dated today.
    assert states["exceptions"] == "pending"
    exc_step = next(s for s in view["steps"] if s["key"] == "exceptions")
    assert "1 open" in exc_step["detail"] and "500.00" in exc_step["detail"]
    assert states["gate"] == "pending"  # the day's pack waits

    approve_pack(store, result["pack_id"], "Priya Nair", POLICY)
    _view, states = _states(store)
    assert states["gate"] == "done"
    assert states["rematch"] == "pending"

    rematch_suspense(store, OfflineReader(), POLICY, "cash-team", DAY)
    _view, states = _states(store)
    assert states["rematch"] == "done"

    record_cancellation(store, "NSP-9002", date(2026, 12, 1), "Priya Nair", DAY)
    view, _states_again = _states(store)
    returns_step = next(s for s in view["steps"] if s["key"] == "returns")
    assert "1 cancellation(s) recorded today" in returns_step["detail"]


def test_today_over_http():
    store = _world()
    client = TestClient(create_app(Service(
        store, InMemoryObjectStore(), OfflineReader(), PolicyConfig())))
    got = client.get("/api/today", params={"date": "2026-09-02"})
    assert got.status_code == 200
    body = got.json()
    assert body["day"] == "2026-09-02" and body["pending"] == 3
    assert [s["tab"] for s in body["steps"]] == [
        "import", "report", "exceptions", "packs", "suspense", "packs"]
    html = client.get("/").text
    assert 'data-tab="today"' in html and "tab-today" in html
