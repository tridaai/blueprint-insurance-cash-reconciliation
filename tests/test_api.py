"""The API shell: health, seed → run → report → gate, over HTTP."""

import pytest
from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.store import InMemoryStore


@pytest.fixture()
def client():
    service = Service(InMemoryStore(), InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    return TestClient(create_app(service))


def test_health_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/readiness").json()
    assert ready["ready"] is True and ready["reader"] == "offline"


def test_console_is_served_with_the_trida_footer(client):
    html = client.get("/").text
    assert "Visit Trida AI →" in html and "View all blueprints →" in html
    assert "trida-rounded.png" in html


def test_seed_run_report_approve_flow(client):
    assert client.post("/api/seed").json()["policies"] == 45
    ran = client.post("/api/days/2026-09-02/run", json={"actor": "cash-team"}).json()
    assert ran["pack_id"] == "PACK-2026-09-02"
    assert ran["report"]["footing_ok"] is True

    refused = client.post("/api/packs/PACK-2026-09-02/approve", json={"approver": "cash-team"})
    assert refused.status_code == 409 and "self-approval" in refused.json()["detail"]

    ok = client.post("/api/packs/PACK-2026-09-02/approve", json={"approver": "Priya Nair"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    posting = client.get("/api/packs/PACK-2026-09-02/posting-file")
    assert posting.status_code == 200 and "NSP-1001" in posting.text

    rep = client.get("/api/days/2026-09-02/report").json()
    assert rep["posted_total"] == "18031.60"


def test_exceptions_and_suspense_endpoints(client):
    client.post("/api/seed")
    client.post("/api/days/2026-09-08/run", json={"actor": "cash-team"})
    exc = client.get("/api/exceptions?status=open").json()
    assert {e["code"] for e in exc} >= {"REMITTANCE_ARITHMETIC", "UNIDENTIFIED"}
    buckets = client.get("/api/suspense?as_of=2026-09-08").json()
    totals = {b["bucket"]: b["total"] for b in buckets}
    assert totals["60+ days"] == "940.00"
