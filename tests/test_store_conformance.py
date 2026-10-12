"""One behavioural battery, every store.

The same assertions run against the in-memory double, the SQLite
local store, and — only when CASH_RECON_TEST_DATABASE_URL names a
throwaway Postgres — the production store. If the three ever drift
apart in behaviour, this module is where it shows up. The rest of
the app cannot tell the stores apart; these tests are why.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.llm import OfflineReader
from cash_recon.models import (
    AuditEvent,
    ExceptionCode,
    ExceptionRecord,
    PostingPack,
    SuspenseItem,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import ingest_dataset, run_day
from cash_recon.store import (
    InMemoryStore,
    PostgresStore,
    SqliteStore,
    make_store,
    store_profile,
)

D = Decimal


def battery(store, tag: str = "") -> None:
    ds = build_dataset()

    # Reference data: Decimal fidelity through the round-trip.
    rec = ds.receivables[2]  # NSP-1003, 9875.50 over 4 installments
    store.upsert_receivable(rec)
    back = store.receivable("NSP-1003")
    assert back.open_balance == D("9875.50")
    assert isinstance(back.installments[0].amount_due, Decimal)
    for broker in ds.brokers[:2]:
        store.upsert_broker(broker)
    assert {b.broker_id for b in store.brokers()} >= {"BRK-ALDER", "BRK-BAYLINE"}

    # Bank lines, listed whole and by date.
    for line in ds.bank_lines:
        store.add_bank_line(line)
    on_0902 = {l.bank_line_id for l in store.bank_lines(on=date(2026, 9, 2))}
    assert on_0902 == {"BL-001", "BL-002", "BL-003", "BL-022"}

    # Remittance: stored, found by content hash, re-saved, embedded.
    rem = next(r for r in ds.remittances if r.remittance_id == "REM-ALDER-01")
    store.add_remittance(rem)
    text_hash = hashlib.sha256(rem.text.encode()).hexdigest()
    assert store.remittance_by_hash(text_hash).remittance_id == "REM-ALDER-01"
    store.save_remittance(rem)
    assert store.remittance_by_hash(text_hash) is not None
    assert {r.remittance_id for r in store.remittances(on=date(2026, 9, 7))} >= {"REM-ALDER-01"}
    store.set_embedding("REM-ALDER-01", [0.1, 0.2, 0.3])
    assert store.embeddings()["REM-ALDER-01"] == pytest.approx([0.1, 0.2, 0.3])

    # Exceptions: added, filtered by status, updated in place.
    exc = ExceptionRecord(
        exception_id=f"EXC-{tag}BATTERY", code=ExceptionCode.PARTIAL,
        bank_line_id="BL-001", policy_number="NSP-1001", broker_id=None,
        amount=D("12.34"), detail="battery case", first_seen=date(2026, 9, 2),
    )
    store.add_exception(exc)
    exc.status = "resolved"
    store.update_exception(exc)
    resolved = [e for e in store.exceptions(status="resolved") if e.exception_id == exc.exception_id]
    assert resolved and resolved[0].amount == D("12.34")

    # Suspense: add is insert-only, save upserts the record.
    item = SuspenseItem(
        suspense_id=f"SUS-{tag}BATTERY", bank_line_id="BL-001", amount=D("50.00"),
        received_date=date(2026, 9, 2), reason="UNIDENTIFIED",
    )
    store.add_suspense(item)
    item.reason = "OVERPAID"
    store.save_suspense(item)
    mine = [i for i in store.suspense_items() if i.suspense_id == item.suspense_id]
    assert mine and mine[0].reason == "OVERPAID" and mine[0].amount == D("50.00")

    # Packs: saved, fetched, listed by date.
    pack = PostingPack(pack_id=f"PACK-{tag}BATTERY", value_date=date(2026, 9, 2),
                       assembled_by="battery")
    store.save_pack(pack)
    assert store.pack(pack.pack_id).assembled_by == "battery"
    assert pack.pack_id in {p.pack_id for p in store.packs(on=date(2026, 9, 2))}

    # Audit: order preserved, subject filter works, timestamps return
    # as datetimes whatever the store keeps underneath.
    store.audit(AuditEvent(at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
                           actor="battery", action="one", subject=f"{tag}S1"))
    store.audit(AuditEvent(at=datetime(2026, 9, 2, 13, tzinfo=timezone.utc),
                           actor="battery", action="two", subject=f"{tag}S2"))
    tagged = [e for e in store.audit_events() if e.subject in (f"{tag}S1", f"{tag}S2")]
    assert [e.action for e in tagged] == ["one", "two"]
    assert [e.action for e in store.audit_events(f"{tag}S2")] == ["two"]
    assert isinstance(tagged[0].at, datetime)


def test_conformance_in_memory():
    battery(InMemoryStore())


def test_conformance_sqlite(tmp_path):
    battery(SqliteStore(tmp_path / "conformance.db"))


@pytest.mark.skipif(
    not os.environ.get("CASH_RECON_TEST_DATABASE_URL"),
    reason="set CASH_RECON_TEST_DATABASE_URL to a throwaway Postgres to run the battery there",
)
def test_conformance_postgres():
    battery(PostgresStore(os.environ["CASH_RECON_TEST_DATABASE_URL"]), tag=uuid.uuid4().hex[:8])


def test_sqlite_money_is_text_never_real(tmp_path):
    path = tmp_path / "money.db"
    SqliteStore(path)
    conn = sqlite3.connect(path)
    types = {row[1]: row[2].upper() for row in conn.execute("PRAGMA table_info(bank_lines)")}
    assert types["amount"] == "TEXT"
    for table in ("allocations", "posting_packs"):
        cols = {row[1]: row[2].upper() for row in conn.execute(f"PRAGMA table_info({table})")}
        assert all(t == "TEXT" for t in cols.values()), (table, cols)
    ddl = "\n".join(
        line.upper()
        for line in Path("migrations/sqlite/0001_core.sql").read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert " REAL" not in ddl and "FLOAT" not in ddl and "NUMERIC" not in ddl


def test_store_selection_is_env_only(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "chosen.db"))
    assert store_profile() == ("sqlite", str(tmp_path / "chosen.db"))
    assert isinstance(make_store(), SqliteStore)

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    assert store_profile() == ("postgres", "postgresql://example.invalid/db")


def test_day_run_on_sqlite_store_end_to_end(tmp_path, monkeypatch):
    """The pipeline itself — graph, checkpointer, report — runs on
    the SQLite profile with no Postgres anywhere."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from cash_recon import pipeline

    db = tmp_path / "run.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SQLITE_PATH", str(db))
    assert isinstance(pipeline._checkpointer(), SqliteSaver)

    store = SqliteStore(db)
    objects = InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    result = run_day(store, objects, OfflineReader(), PolicyConfig(),
                     date(2026, 9, 2), actor="cash-team")
    assert result["pack_id"] == "PACK-2026-09-02"
    assert result["report"]["footing_ok"] is True
    # And the state really persisted: a second store over the file
    # sees the pack and the day's allocations.
    reopened = SqliteStore(db)
    assert reopened.pack("PACK-2026-09-02") is not None
    assert reopened.allocations()
