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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.documents import EMBEDDING_DIM
from cash_recon.llm import OfflineReader
from cash_recon.models import (
    Allocation,
    AuditEvent,
    BankLine,
    Cancellation,
    DayClaim,
    ExceptionCode,
    ExceptionRecord,
    PayableEvent,
    PaymentReturn,
    PayoutEntry,
    PayoutPack,
    PostingPack,
    ReturnPremiumPayable,
    ReturnReason,
    SuspenseItem,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import ingest_dataset, run_day
from cash_recon.store import (
    EmbeddingDimensionError,
    InMemoryStore,
    PostgresStore,
    SqliteStore,
    make_store,
    store_profile,
)

D = Decimal

# The embedding contract is EMBEDDING_DIM (384) on every leg — the
# Postgres column is vector(384), so the battery honours the real
# contract instead of a toy vector the schema would reject. The
# values are sixteenths: exact at the six decimals the Postgres
# store formats its pgvector literal with, so the round-trip is
# exact on all three stores.
BATTERY_VECTOR = [((i % 8) + 1) / 16.0 for i in range(EMBEDDING_DIM)]


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
    store.set_embedding("REM-ALDER-01", BATTERY_VECTOR)
    back_vector = store.embeddings()["REM-ALDER-01"]
    assert len(back_vector) == EMBEDDING_DIM
    assert back_vector == pytest.approx(BATTERY_VECTOR)

    # The dimension contract is enforced by the store, in code, on
    # every leg: a wrong-dimension vector is refused with the
    # deliberate error — naming the expected dimension — and the
    # stored vector is untouched.
    with pytest.raises(EmbeddingDimensionError, match=str(EMBEDDING_DIM)):
        store.set_embedding("REM-ALDER-01", [0.1, 0.2, 0.3])
    assert store.embeddings()["REM-ALDER-01"] == pytest.approx(BATTERY_VECTOR)

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
    # A second add of the same id changes nothing on any store: the
    # record on the ledger stands (a re-run cannot resurrect or
    # re-price it by adding again).
    store.add_suspense(SuspenseItem(
        suspense_id=item.suspense_id, bank_line_id="BL-001", amount=D("99.00"),
        received_date=date(2026, 9, 2), reason="UNIDENTIFIED", status="open",
    ))
    mine = [i for i in store.suspense_items() if i.suspense_id == item.suspense_id]
    assert len(mine) == 1 and mine[0].amount == D("50.00") and mine[0].reason == "OVERPAID"

    # Packs: saved, fetched, listed by date.
    pack = PostingPack(pack_id=f"PACK-{tag}BATTERY", value_date=date(2026, 9, 2),
                       assembled_by="battery")
    store.save_pack(pack)
    assert store.pack(pack.pack_id).assembled_by == "battery"
    assert pack.pack_id in {p.pack_id for p in store.packs(on=date(2026, 9, 2))}

    # Bank lines: save upserts the record (the re-send link lands
    # this way) while add stays insert-only. The line is EUR: the
    # denomination round-trips on every store, payload and all.
    bl = BankLine(bank_line_id=f"BL-{tag}BATTERY", value_date=date(2026, 9, 3),
                  amount=D("10.00"), payer_name="Battery Payer", reference=None,
                  currency="EUR")
    store.add_bank_line(bl)
    bl.resend_of = "BL-001"
    store.save_bank_line(bl)
    saved = [l for l in store.bank_lines(on=date(2026, 9, 3)) if l.bank_line_id == bl.bank_line_id]
    assert saved and saved[0].resend_of == "BL-001" and saved[0].currency == "EUR"

    # Allocations: saved under a pack; a gate edit replaces the
    # pack's rows wholesale so the index follows the pack. The
    # allocation carries its recorded conversion with it.
    alloc = Allocation(bank_line_id=f"BL-{tag}ALLOC", policy_number="NSP-1001",
                       installment_id="NSP-1001-I1", amount=D("10.00"), tier=None,
                       currency="EUR", fx_rate=D("1.08"), functional_amount=D("10.80"))
    store.save_allocations([alloc], f"PACK-{tag}BATTERY")
    assert [a.amount for a in store.allocations(alloc.bank_line_id)] == [D("10.00")]
    # Saving the same (bank line, installment) again indexes nothing
    # twice, on any store — the pair is the index's key, not the call.
    store.save_allocations([alloc], f"PACK-{tag}BATTERY")
    assert [a.amount for a in store.allocations(alloc.bank_line_id)] == [D("10.00")]
    alloc.amount = D("12.00")
    store.replace_allocations([alloc], f"PACK-{tag}BATTERY")
    mine = store.allocations(alloc.bank_line_id)
    assert len(mine) == 1 and mine[0].amount == D("12.00")
    assert mine[0].currency == "EUR" and mine[0].fx_rate == D("1.08")
    assert mine[0].functional_amount == D("10.80")

    # Payment returns: recorded, listed by date, found by line.
    ret = PaymentReturn(
        return_id=f"RET-{tag}BATTERY", bank_line_id="BL-001", reason=ReturnReason.NSF,
        return_date=date(2026, 9, 4), recorded_by="battery", amount=D("4800.00"),
        pack_ids=["PACK-2026-09-02"],
    )
    store.add_return(ret)
    assert store.return_for_line("BL-001").reason == ReturnReason.NSF
    assert store.return_for_line("BL-002") is None
    assert ret.return_id in {r.return_id for r in store.returns(on=date(2026, 9, 4))}
    mine = [r for r in store.returns() if r.return_id == ret.return_id]
    assert mine and isinstance(mine[0].amount, Decimal)

    # A line's returns, in order: part-returns (round 11) mean a
    # line can carry more than one return record, and
    # return_for_line stays the first of them on every store.
    ret2 = PaymentReturn(
        return_id=f"RET-{tag}BATTERY-2", bank_line_id="BL-001", reason=ReturnReason.NSF,
        return_date=date(2026, 9, 5), recorded_by="battery", amount=D("100.00"),
        partial=True,
    )
    store.add_return(ret2)
    # The Postgres leg shares one database across runs, so the
    # line's full list accumulates earlier runs' records: this
    # run's pair, in order, is what the semantics pin.
    pair = [r.return_id for r in store.returns_for_line("BL-001")
            if r.return_id in (ret.return_id, ret2.return_id)]
    assert pair == [ret.return_id, ret2.return_id]
    assert store.return_for_line("BL-001").return_id == \
        store.returns_for_line("BL-001")[0].return_id
    assert store.returns_for_line("BL-002") == []

    # Return premiums: the cancellation, its payable (saved, moved,
    # re-read with its chain intact), and a payout pack.
    cxl = Cancellation(
        cancellation_id=f"CXL-{tag}BATTERY", policy_number="NSP-1003",
        effective_date=date(2026, 12, 1), recorded_on=date(2026, 12, 1),
        recorded_by="battery", premium_total=D("9875.50"), term_days=365,
        elapsed_days=91, earned_premium=D("2461.00"), paid_to_date=D("4937.76"),
        return_premium=D("2476.76"), insured_owes=D("0.00"),
        payee_kind="broker", payee_name="Bayline Risk Partners",
        broker_id="BRK-BAYLINE", payable_id=f"PAY-{tag}BATTERY",
        computation=["battery computation"],
    )
    store.add_cancellation(cxl)
    assert store.cancellation_for("NSP-1003").return_premium == D("2476.76")
    assert store.cancellation_for("NSP-9999") is None
    payable = ReturnPremiumPayable(
        payable_id=f"PAY-{tag}BATTERY", policy_number="NSP-1003",
        cancellation_id=cxl.cancellation_id, payee_kind="broker",
        payee_name="Bayline Risk Partners", broker_id="BRK-BAYLINE",
        amount=D("2476.76"), opened_on=date(2026, 12, 1),
        events=[PayableEvent(on=date(2026, 12, 1), kind="opened",
                             amount=D("2476.76"), ref=cxl.cancellation_id)],
    )
    store.save_payable(payable)
    payable.amount_netted = D("476.76")
    payable.events.append(PayableEvent(on=date(2026, 12, 3), kind="netted",
                                       amount=D("476.76"), ref="BL-001"))
    store.save_payable(payable)
    back_payable = store.payable_for_policy("NSP-1003")
    assert back_payable.remaining == D("2000.00")
    assert [e.kind for e in back_payable.events] == ["opened", "netted"]
    assert store.payable(back_payable.payable_id).status == "open"
    assert store.payables(status="open") and not store.payables(status="paid")
    payout = PayoutPack(
        pack_id=f"PAYOUT-{tag}BATTERY", value_date=date(2026, 12, 4),
        assembled_by="battery",
        entries=[PayoutEntry(payable_id=payable.payable_id, policy_number="NSP-1003",
                             payee_kind="broker", payee_name="Bayline Risk Partners",
                             broker_id="BRK-BAYLINE", amount=D("2000.00"))],
    )
    store.save_payout_pack(payout)
    assert store.payout_pack(payout.pack_id).total == D("2000.00")
    assert payout.pack_id in {p.pack_id for p in store.payout_packs(on=date(2026, 12, 4))}

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

    # Day claims (migration 0006): the first starter wins the day
    # atomically, on every store. The scratch day is far outside
    # the dataset; the Postgres leg shares one database across
    # runs, so its day is derived from the run's tag.
    claim_day = date(2031, 1, 1) + timedelta(days=(int(tag, 16) % 2000) if tag else 0)
    first = DayClaim(day=claim_day, attempt=1, actor="battery-one",
                     claimed_at=datetime(2031, 1, 1, tzinfo=timezone.utc),
                     state="running", thread_id=f"day-{claim_day.isoformat()}-a1")
    assert store.claim_day(first) is True
    assert store.claim_day(first.model_copy(update={"actor": "battery-two"})) is False
    standing = store.day_claim(claim_day)
    assert standing is not None and standing.actor == "battery-one"
    assert standing.attempt == 1 and standing.state == "running"
    assert standing.thread_id == f"day-{claim_day.isoformat()}-a1"
    assert store.day_claim(claim_day + timedelta(days=1)) is None
    # The day's run lock: one holder at a time, released cleanly.
    assert store.try_day_lock(claim_day) is True
    assert store.try_day_lock(claim_day) is False
    store.release_day_lock(claim_day)
    assert store.try_day_lock(claim_day) is True
    store.release_day_lock(claim_day)
    # Reclaim is compare-and-swap on the interrupted attempt's
    # number: the wrong number loses, the right one takes over.
    second = DayClaim(day=claim_day, attempt=2, actor="battery-two",
                      claimed_at=datetime(2031, 1, 1, tzinfo=timezone.utc),
                      state="running", thread_id=f"day-{claim_day.isoformat()}-a2")
    assert store.reclaim_day(second, from_attempt=99) is False
    assert store.day_claim(claim_day).attempt == 1
    assert store.reclaim_day(second, from_attempt=1) is True
    assert store.day_claim(claim_day).actor == "battery-two"
    # A completed claim is the day's standing record: it cannot be
    # reclaimed, and the day cannot be claimed anew.
    second.state = "completed"
    store.save_day_claim(second)
    assert store.day_claim(claim_day).state == "completed"
    third = second.model_copy(update={"attempt": 3, "state": "running"})
    assert store.reclaim_day(third, from_attempt=2) is False
    assert store.claim_day(third) is False
    assert store.day_claim(claim_day).attempt == 2


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


@pytest.mark.skipif(
    not os.environ.get("CASH_RECON_TEST_DATABASE_URL"),
    reason="set CASH_RECON_TEST_DATABASE_URL to a throwaway Postgres to run the race test there",
)
def test_postgres_day_claim_two_writer_race():
    """The round-10 gap, closed against the real store: two store
    instances (two connections, two processes' worth of state)
    race for the same day, released at the same moment — exactly
    one wins the claim, exactly one holds the run lock, and the
    lock passes to the loser only when the winner releases it."""
    import threading

    dsn = os.environ["CASH_RECON_TEST_DATABASE_URL"]
    store_a, store_b = PostgresStore(dsn), PostgresStore(dsn)
    day = date(2031, 1, 1) + timedelta(days=int(uuid.uuid4().hex[:8], 16) % 2000)

    def race(fn):
        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}

        def go(name, store):
            barrier.wait()
            results[name] = fn(store)

        threads = [
            threading.Thread(target=go, args=("a", store_a)),
            threading.Thread(target=go, args=("b", store_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    claims = race(lambda s: s.claim_day(DayClaim(
        day=day, attempt=1, actor="racer",
        claimed_at=datetime.now(timezone.utc), state="running")))
    assert sorted(claims.values()) == [False, True]

    locks = race(lambda s: s.try_day_lock(day))
    assert sorted(locks.values()) == [False, True]
    winner = store_a if locks["a"] else store_b
    loser = store_b if locks["a"] else store_a
    winner.release_day_lock(day)
    assert loser.try_day_lock(day) is True
    loser.release_day_lock(day)


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
