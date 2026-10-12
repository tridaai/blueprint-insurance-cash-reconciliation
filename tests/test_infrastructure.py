"""Stores and object storage: the doubles keep Decimal fidelity, the
S3 client signs the way AWS expects, documents round-trip."""

import hashlib
import hmac
from datetime import date
from decimal import Decimal

from cash_recon.dataset import build_dataset
from cash_recon.objects import InMemoryObjectStore, _signature_key
from cash_recon.store import InMemoryStore

D = Decimal


def test_in_memory_store_round_trips_money_exactly():
    ds = build_dataset()
    store = InMemoryStore()
    rec = ds.receivables[2]  # NSP-1003, 9875.50 over 4 installments
    store.upsert_receivable(rec)
    back = store.receivable("NSP-1003")
    assert back.open_balance == D("9875.50")
    assert [i.amount_due for i in back.installments] == [D("2468.88"), D("2468.88"), D("2468.87"), D("2468.87")]
    assert isinstance(back.installments[0].amount_due, Decimal)


def test_store_copies_protect_the_ledger():
    ds = build_dataset()
    store = InMemoryStore()
    store.upsert_receivable(ds.receivables[0])
    leaked = store.receivable("NSP-1001")
    leaked.installments[0].amount_applied = D("999.00")  # mutate the copy
    assert store.receivable("NSP-1001").installments[0].amount_applied == D("0.00")


def test_object_store_round_trip():
    objects = InMemoryObjectStore()
    objects.put("remittances/REM-X.txt", b"advice text")
    assert objects.exists("remittances/REM-X.txt")
    assert objects.get("remittances/REM-X.txt") == b"advice text"
    assert not objects.exists("remittances/NOPE.txt")


def test_remittance_object_written_at_ingest(world):
    _ds, _store, objects = world
    assert objects.get("remittances/REM-ALDER-01.txt").decode().startswith("Remittance advice")


def test_sigv4_signature_key_derivation():
    # The derivation chain is deterministic: AWS4(secret) -> date ->
    # region -> service -> aws4_request.
    key = _signature_key("secret", "20260902", "us-east-1")
    manual = hmac.new(("AWS4" + "secret").encode(), b"20260902", hashlib.sha256).digest()
    manual = hmac.new(manual, b"us-east-1", hashlib.sha256).digest()
    manual = hmac.new(manual, b"s3", hashlib.sha256).digest()
    manual = hmac.new(manual, b"aws4_request", hashlib.sha256).digest()
    assert key == manual


def test_migrations_are_numbered_and_present():
    from pathlib import Path

    files = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert files == ["0001_core.sql", "0002_document_embeddings.sql"]
    assert "CREATE EXTENSION" in Path("migrations/0001_core.sql").read_text()
    # The SQLite dialect mirrors the same numbered set, in its own
    # directory: same semantics, no pgvector, no server extensions.
    sqlite_files = sorted(p.name for p in Path("migrations/sqlite").glob("*.sql"))
    assert sqlite_files == files
    sqlite_core = "\n".join(
        line for line in Path("migrations/sqlite/0001_core.sql").read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "CREATE EXTENSION" not in sqlite_core and "JSONB" not in sqlite_core
