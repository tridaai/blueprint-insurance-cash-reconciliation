"""Persistence behind one interface.

Three implementations of the same protocol:

- **PostgreSQL** — production. JSONB payloads, numbered SQL migrations
  in `migrations/` run at startup, pgvector for document embeddings.
- **SQLite** — the local default. One file (``./data/cashrecon.db``
  unless `SQLITE_PATH` says otherwise), the same tables and payload
  shapes via the SQLite-dialect migrations in `migrations/sqlite/`,
  money held as decimal TEXT (never REAL), and document embeddings
  stored as JSON with similarity computed in code — no pgvector, and
  no database server to install, start, or configure.
- **In-memory** — a test double for tests and evals, never the
  production or local story.

Selection happens in exactly one place: `make_store()` (via
`store_profile()`). `DATABASE_URL` set means Postgres; otherwise the
SQLite file. Nothing else in the app knows which store is running.

Amounts survive the round-trip as decimal strings inside the JSON
payloads and are re-validated into the Pydantic models on the way
out, so a Decimal that went in comes back a Decimal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol

from .models import (
    Allocation,
    AuditEvent,
    BankLine,
    Broker,
    ExceptionRecord,
    PostingPack,
    Receivable,
    Remittance,
    SuspenseItem,
)

DEFAULT_SQLITE_PATH = "data/cashrecon.db"


def _default_migrations_dir(dialect: str = "") -> Path:
    """Locate the numbered migrations for a dialect ("" = PostgreSQL,
    "sqlite" = SQLite). `./migrations` relative to the working
    directory wins (the compose image and a repo checkout both run
    from the root); otherwise the path anchored at this package's
    repo root, so the CLI works from any directory in a checkout."""
    local = Path("migrations") / dialect if dialect else Path("migrations")
    if local.exists():
        return local
    return Path(__file__).resolve().parents[2] / "migrations" / dialect


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not serialisable: {type(obj)!r}")


def dump(model) -> str:
    return json.dumps(model.model_dump(), default=_json_default)


class Store(Protocol):
    def upsert_broker(self, broker: Broker) -> None: ...
    def brokers(self) -> list[Broker]: ...
    def upsert_receivable(self, rec: Receivable) -> None: ...
    def receivables(self) -> list[Receivable]: ...
    def receivable(self, policy_number: str) -> Receivable | None: ...
    def add_bank_line(self, line: BankLine) -> None: ...
    def bank_lines(self, on: date | None = None) -> list[BankLine]: ...
    def add_remittance(self, rem: Remittance) -> None: ...
    def save_remittance(self, rem: Remittance) -> None: ...
    def remittances(self, on: date | None = None) -> list[Remittance]: ...
    def remittance_by_hash(self, text_hash: str) -> Remittance | None: ...
    def set_embedding(self, remittance_id: str, vector: list[float]) -> None: ...
    def embeddings(self) -> dict[str, list[float]]: ...
    def save_allocations(self, allocs: list[Allocation], pack_id: str | None) -> None: ...
    def allocations(self, bank_line_id: str | None = None) -> list[Allocation]: ...
    def add_exception(self, exc: ExceptionRecord) -> None: ...
    def exceptions(self, status: str | None = None) -> list[ExceptionRecord]: ...
    def update_exception(self, exc: ExceptionRecord) -> None: ...
    def add_suspense(self, item: SuspenseItem) -> None: ...
    def save_suspense(self, item: SuspenseItem) -> None: ...
    def suspense_items(self) -> list[SuspenseItem]: ...
    def save_pack(self, pack: PostingPack) -> None: ...
    def pack(self, pack_id: str) -> PostingPack | None: ...
    def packs(self, on: date | None = None) -> list[PostingPack]: ...
    def audit(self, event: AuditEvent) -> None: ...
    def audit_events(self, subject: str | None = None) -> list[AuditEvent]: ...


class InMemoryStore:
    """Test double implementing the Store protocol in plain dicts."""

    profile_name = "memory"

    def __init__(self) -> None:
        self._brokers: dict[str, Broker] = {}
        self._receivables: dict[str, Receivable] = {}
        self._bank_lines: dict[str, BankLine] = {}
        self._remittances: dict[str, Remittance] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._allocations: list[Allocation] = []
        self._exceptions: dict[str, ExceptionRecord] = {}
        self._suspense: dict[str, SuspenseItem] = {}
        self._packs: dict[str, PostingPack] = {}
        self._audit: list[AuditEvent] = []

    # -- reference data -------------------------------------------------
    def upsert_broker(self, broker):
        self._brokers[broker.broker_id] = copy.deepcopy(broker)

    def brokers(self):
        return copy.deepcopy(list(self._brokers.values()))

    def upsert_receivable(self, rec):
        self._receivables[rec.policy_number] = copy.deepcopy(rec)

    def receivables(self):
        return copy.deepcopy(list(self._receivables.values()))

    def receivable(self, policy_number):
        return copy.deepcopy(self._receivables.get(policy_number))

    # -- inputs -----------------------------------------------------------
    def add_bank_line(self, line):
        self._bank_lines[line.bank_line_id] = copy.deepcopy(line)

    def bank_lines(self, on=None):
        rows = copy.deepcopy(list(self._bank_lines.values()))
        return [r for r in rows if on is None or r.value_date == on]

    def add_remittance(self, rem):
        self._remittances[rem.remittance_id] = copy.deepcopy(rem)

    def save_remittance(self, rem):
        self._remittances[rem.remittance_id] = copy.deepcopy(rem)

    def remittances(self, on=None):
        rows = copy.deepcopy(list(self._remittances.values()))
        return [r for r in rows if on is None or r.received_date == on]

    def remittance_by_hash(self, text_hash):
        for rem in self._remittances.values():
            if hashlib.sha256(rem.text.encode()).hexdigest() == text_hash:
                return copy.deepcopy(rem)
        return None

    def set_embedding(self, remittance_id, vector):
        self._embeddings[remittance_id] = vector

    def embeddings(self):
        return dict(self._embeddings)

    # -- outcomes -----------------------------------------------------------
    def save_allocations(self, allocs, pack_id):
        self._allocations.extend(copy.deepcopy(allocs))

    def allocations(self, bank_line_id=None):
        if bank_line_id is None:
            return copy.deepcopy(list(self._allocations))
        return copy.deepcopy([a for a in self._allocations if a.bank_line_id == bank_line_id])

    def add_exception(self, exc):
        self._exceptions[exc.exception_id] = copy.deepcopy(exc)

    def exceptions(self, status=None):
        rows = copy.deepcopy(list(self._exceptions.values()))
        return [r for r in rows if status is None or r.status == status]

    def update_exception(self, exc):
        self._exceptions[exc.exception_id] = copy.deepcopy(exc)

    def add_suspense(self, item):
        self._suspense[item.suspense_id] = copy.deepcopy(item)

    def save_suspense(self, item):
        self._suspense[item.suspense_id] = copy.deepcopy(item)

    def suspense_items(self):
        return copy.deepcopy(list(self._suspense.values()))

    def save_pack(self, pack):
        self._packs[pack.pack_id] = copy.deepcopy(pack)

    def pack(self, pack_id):
        return copy.deepcopy(self._packs.get(pack_id))

    def packs(self, on=None):
        rows = copy.deepcopy(list(self._packs.values()))
        return [r for r in rows if on is None or r.value_date == on]

    def audit(self, event):
        self._audit.append(copy.deepcopy(event))

    def audit_events(self, subject=None):
        if subject is None:
            return copy.deepcopy(list(self._audit))
        return copy.deepcopy([e for e in self._audit if e.subject == subject])


class PostgresStore:
    """The production store. Applies ./migrations on construction."""

    profile_name = "postgres"

    def __init__(self, dsn: str, migrations_dir: str | Path | None = None) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._dict_row = dict_row
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        base = Path(migrations_dir) if migrations_dir else _default_migrations_dir()
        self._migrate(base)

    def _migrate(self, base: Path) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in cur.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in sorted(base.glob("[0-9]*.sql")):
            if path.name in done:
                continue
            cur.execute(path.read_text())
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
            )

    # -- helpers -----------------------------------------------------------
    def _one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def _put(self, sql, params):
        self.conn.execute(sql, params)

    # -- reference data ------------------------------------------------------
    def upsert_broker(self, broker):
        self._put(
            "INSERT INTO brokers (broker_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (broker_id) DO UPDATE SET payload = EXCLUDED.payload",
            (broker.broker_id, dump(broker)),
        )

    def brokers(self):
        return [Broker.model_validate(r["payload"]) for r in self._all("SELECT payload FROM brokers")]

    def upsert_receivable(self, rec):
        self._put(
            "INSERT INTO receivables (policy_number, broker_id, status, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (policy_number) DO UPDATE SET "
            "broker_id = EXCLUDED.broker_id, status = EXCLUDED.status, payload = EXCLUDED.payload",
            (rec.policy_number, rec.broker_id, rec.status.value, dump(rec)),
        )

    def receivables(self):
        return [Receivable.model_validate(r["payload"]) for r in self._all("SELECT payload FROM receivables")]

    def receivable(self, policy_number):
        row = self._one("SELECT payload FROM receivables WHERE policy_number = %s", (policy_number,))
        return Receivable.model_validate(row["payload"]) if row else None

    # -- inputs --------------------------------------------------------------
    def add_bank_line(self, line):
        self._put(
            "INSERT INTO bank_lines (bank_line_id, value_date, amount, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (bank_line_id) DO NOTHING",
            (line.bank_line_id, line.value_date, line.amount, dump(line)),
        )

    def bank_lines(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM bank_lines ORDER BY value_date, bank_line_id")
        else:
            rows = self._all(
                "SELECT payload FROM bank_lines WHERE value_date = %s ORDER BY bank_line_id", (on,)
            )
        return [BankLine.model_validate(r["payload"]) for r in rows]

    def add_remittance(self, rem):
        self._put(
            "INSERT INTO remittances (remittance_id, received_date, text_hash, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (remittance_id) DO NOTHING",
            (
                rem.remittance_id,
                rem.received_date,
                hashlib.sha256(rem.text.encode()).hexdigest(),
                dump(rem),
            ),
        )

    def save_remittance(self, rem):
        self._put(
            "INSERT INTO remittances (remittance_id, received_date, text_hash, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (remittance_id) DO UPDATE SET "
            "received_date = EXCLUDED.received_date, payload = EXCLUDED.payload",
            (
                rem.remittance_id,
                rem.received_date,
                hashlib.sha256(rem.text.encode()).hexdigest(),
                dump(rem),
            ),
        )

    def remittances(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM remittances ORDER BY received_date, remittance_id")
        else:
            rows = self._all(
                "SELECT payload FROM remittances WHERE received_date = %s ORDER BY remittance_id", (on,)
            )
        return [Remittance.model_validate(r["payload"]) for r in rows]

    def remittance_by_hash(self, text_hash):
        row = self._one("SELECT payload FROM remittances WHERE text_hash = %s", (text_hash,))
        return Remittance.model_validate(row["payload"]) if row else None

    def set_embedding(self, remittance_id, vector):
        literal = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        self._put(
            "INSERT INTO remittance_embeddings (remittance_id, embedding) VALUES (%s, %s::vector) "
            "ON CONFLICT (remittance_id) DO UPDATE SET embedding = EXCLUDED.embedding",
            (remittance_id, literal),
        )

    def embeddings(self):
        rows = self._all("SELECT remittance_id, embedding::text AS emb FROM remittance_embeddings")
        out = {}
        for r in rows:
            out[r["remittance_id"]] = [float(x) for x in r["emb"].strip("[]").split(",") if x]
        return out

    # -- outcomes --------------------------------------------------------------
    def save_allocations(self, allocs, pack_id):
        for a in allocs:
            self._put(
                "INSERT INTO allocations (bank_line_id, policy_number, installment_id, amount, tier, pack_id, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (bank_line_id, installment_id) DO NOTHING",
                (a.bank_line_id, a.policy_number, a.installment_id, a.amount, a.tier.value if a.tier else None, pack_id, dump(a)),
            )

    def allocations(self, bank_line_id=None):
        if bank_line_id is None:
            rows = self._all("SELECT payload FROM allocations")
        else:
            rows = self._all("SELECT payload FROM allocations WHERE bank_line_id = %s", (bank_line_id,))
        return [Allocation.model_validate(r["payload"]) for r in rows]

    def add_exception(self, exc):
        self._put(
            "INSERT INTO exceptions (exception_id, code, status, first_seen, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (exception_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload",
            (exc.exception_id, exc.code.value, exc.status, exc.first_seen, dump(exc)),
        )

    def exceptions(self, status=None):
        if status is None:
            rows = self._all("SELECT payload FROM exceptions ORDER BY first_seen, exception_id")
        else:
            rows = self._all("SELECT payload FROM exceptions WHERE status = %s ORDER BY first_seen, exception_id", (status,))
        return [ExceptionRecord.model_validate(r["payload"]) for r in rows]

    def update_exception(self, exc):
        self.add_exception(exc)

    def add_suspense(self, item):
        self._put(
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (suspense_id) DO NOTHING",
            (item.suspense_id, item.bank_line_id, item.received_date, dump(item)),
        )

    def save_suspense(self, item):
        self._put(
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (suspense_id) DO UPDATE SET payload = EXCLUDED.payload",
            (item.suspense_id, item.bank_line_id, item.received_date, dump(item)),
        )

    def suspense_items(self):
        return [SuspenseItem.model_validate(r["payload"]) for r in self._all("SELECT payload FROM suspense_items")]

    def save_pack(self, pack):
        self._put(
            "INSERT INTO posting_packs (pack_id, value_date, status, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (pack_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload",
            (pack.pack_id, pack.value_date, pack.status.value, dump(pack)),
        )

    def pack(self, pack_id):
        row = self._one("SELECT payload FROM posting_packs WHERE pack_id = %s", (pack_id,))
        return PostingPack.model_validate(row["payload"]) if row else None

    def packs(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM posting_packs ORDER BY value_date, pack_id")
        else:
            rows = self._all("SELECT payload FROM posting_packs WHERE value_date = %s ORDER BY pack_id", (on,))
        return [PostingPack.model_validate(r["payload"]) for r in rows]

    def audit(self, event):
        self._put(
            "INSERT INTO audit_events (at, actor, action, subject, detail) VALUES (%s, %s, %s, %s, %s)",
            (event.at, event.actor, event.action, event.subject, event.detail),
        )

    def audit_events(self, subject=None):
        if subject is None:
            rows = self._all("SELECT at, actor, action, subject, detail FROM audit_events ORDER BY id")
        else:
            rows = self._all(
                "SELECT at, actor, action, subject, detail FROM audit_events WHERE subject = %s ORDER BY id",
                (subject,),
            )
        return [AuditEvent.model_validate(r) for r in rows]


class SqliteStore:
    """The local store: one SQLite file, zero database setup.

    Mirrors the Postgres store table for table, using the
    SQLite-dialect migrations in `migrations/sqlite/`. Payloads are
    JSON text; money key columns are TEXT decimal strings — SQLite's
    NUMERIC/REAL affinity never touches a cent. Document embeddings
    are stored as JSON arrays; the near-duplicate comparison runs in
    code over the stored vectors (documents.py), which at this scale
    gives exactly the pgvector path's answers with different
    machinery.
    """

    profile_name = "sqlite"

    def __init__(self, path: str | Path, migrations_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        base = Path(migrations_dir) if migrations_dir else _default_migrations_dir("sqlite")
        self._migrate(base)

    def _migrate(self, base: Path) -> None:
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(base.glob("[0-9]*.sql")):
                if path.name in done:
                    continue
                self.conn.executescript(path.read_text())
                self.conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (path.name,)
                )
            self.conn.commit()

    # -- helpers -----------------------------------------------------------
    def _one(self, sql, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def _all(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _put(self, sql, params):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    @staticmethod
    def _payload(row) -> dict:
        return json.loads(row["payload"])

    # -- reference data ------------------------------------------------------
    def upsert_broker(self, broker):
        self._put(
            "INSERT INTO brokers (broker_id, payload) VALUES (?, ?) "
            "ON CONFLICT (broker_id) DO UPDATE SET payload = excluded.payload",
            (broker.broker_id, dump(broker)),
        )

    def brokers(self):
        return [Broker.model_validate(self._payload(r)) for r in self._all("SELECT payload FROM brokers")]

    def upsert_receivable(self, rec):
        self._put(
            "INSERT INTO receivables (policy_number, broker_id, status, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (policy_number) DO UPDATE SET "
            "broker_id = excluded.broker_id, status = excluded.status, payload = excluded.payload",
            (rec.policy_number, rec.broker_id, rec.status.value, dump(rec)),
        )

    def receivables(self):
        return [Receivable.model_validate(self._payload(r)) for r in self._all("SELECT payload FROM receivables")]

    def receivable(self, policy_number):
        row = self._one("SELECT payload FROM receivables WHERE policy_number = ?", (policy_number,))
        return Receivable.model_validate(self._payload(row)) if row else None

    # -- inputs --------------------------------------------------------------
    def add_bank_line(self, line):
        self._put(
            "INSERT OR IGNORE INTO bank_lines (bank_line_id, value_date, amount, payload) "
            "VALUES (?, ?, ?, ?)",
            (line.bank_line_id, line.value_date.isoformat(), str(line.amount), dump(line)),
        )

    def bank_lines(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM bank_lines ORDER BY value_date, bank_line_id")
        else:
            rows = self._all(
                "SELECT payload FROM bank_lines WHERE value_date = ? ORDER BY bank_line_id",
                (on.isoformat(),),
            )
        return [BankLine.model_validate(self._payload(r)) for r in rows]

    def add_remittance(self, rem):
        self._put(
            "INSERT OR IGNORE INTO remittances (remittance_id, received_date, text_hash, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                rem.remittance_id,
                rem.received_date.isoformat(),
                hashlib.sha256(rem.text.encode()).hexdigest(),
                dump(rem),
            ),
        )

    def save_remittance(self, rem):
        self._put(
            "INSERT INTO remittances (remittance_id, received_date, text_hash, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (remittance_id) DO UPDATE SET "
            "received_date = excluded.received_date, payload = excluded.payload",
            (
                rem.remittance_id,
                rem.received_date.isoformat(),
                hashlib.sha256(rem.text.encode()).hexdigest(),
                dump(rem),
            ),
        )

    def remittances(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM remittances ORDER BY received_date, remittance_id")
        else:
            rows = self._all(
                "SELECT payload FROM remittances WHERE received_date = ? ORDER BY remittance_id",
                (on.isoformat(),),
            )
        return [Remittance.model_validate(self._payload(r)) for r in rows]

    def remittance_by_hash(self, text_hash):
        row = self._one("SELECT payload FROM remittances WHERE text_hash = ?", (text_hash,))
        return Remittance.model_validate(self._payload(row)) if row else None

    def set_embedding(self, remittance_id, vector):
        self._put(
            "INSERT INTO remittance_embeddings (remittance_id, embedding) VALUES (?, ?) "
            "ON CONFLICT (remittance_id) DO UPDATE SET embedding = excluded.embedding",
            (remittance_id, json.dumps(vector)),
        )

    def embeddings(self):
        rows = self._all("SELECT remittance_id, embedding FROM remittance_embeddings")
        return {r["remittance_id"]: [float(x) for x in json.loads(r["embedding"])] for r in rows}

    # -- outcomes --------------------------------------------------------------
    def save_allocations(self, allocs, pack_id):
        for a in allocs:
            self._put(
                "INSERT OR IGNORE INTO allocations (bank_line_id, policy_number, installment_id, amount, tier, pack_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a.bank_line_id, a.policy_number, a.installment_id, str(a.amount),
                 a.tier.value if a.tier else None, pack_id, dump(a)),
            )

    def allocations(self, bank_line_id=None):
        if bank_line_id is None:
            rows = self._all("SELECT payload FROM allocations")
        else:
            rows = self._all("SELECT payload FROM allocations WHERE bank_line_id = ?", (bank_line_id,))
        return [Allocation.model_validate(self._payload(r)) for r in rows]

    def add_exception(self, exc):
        self._put(
            "INSERT INTO exceptions (exception_id, code, status, first_seen, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (exception_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload",
            (exc.exception_id, exc.code.value, exc.status, exc.first_seen.isoformat(), dump(exc)),
        )

    def exceptions(self, status=None):
        if status is None:
            rows = self._all("SELECT payload FROM exceptions ORDER BY first_seen, exception_id")
        else:
            rows = self._all(
                "SELECT payload FROM exceptions WHERE status = ? ORDER BY first_seen, exception_id",
                (status,),
            )
        return [ExceptionRecord.model_validate(self._payload(r)) for r in rows]

    def update_exception(self, exc):
        self.add_exception(exc)

    def add_suspense(self, item):
        self._put(
            "INSERT OR IGNORE INTO suspense_items (suspense_id, bank_line_id, received_date, payload) "
            "VALUES (?, ?, ?, ?)",
            (item.suspense_id, item.bank_line_id, item.received_date.isoformat(), dump(item)),
        )

    def save_suspense(self, item):
        self._put(
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (suspense_id) DO UPDATE SET payload = excluded.payload",
            (item.suspense_id, item.bank_line_id, item.received_date.isoformat(), dump(item)),
        )

    def suspense_items(self):
        return [SuspenseItem.model_validate(self._payload(r)) for r in self._all("SELECT payload FROM suspense_items")]

    def save_pack(self, pack):
        self._put(
            "INSERT INTO posting_packs (pack_id, value_date, status, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (pack_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload",
            (pack.pack_id, pack.value_date.isoformat(), pack.status.value, dump(pack)),
        )

    def pack(self, pack_id):
        row = self._one("SELECT payload FROM posting_packs WHERE pack_id = ?", (pack_id,))
        return PostingPack.model_validate(self._payload(row)) if row else None

    def packs(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM posting_packs ORDER BY value_date, pack_id")
        else:
            rows = self._all(
                "SELECT payload FROM posting_packs WHERE value_date = ? ORDER BY pack_id",
                (on.isoformat(),),
            )
        return [PostingPack.model_validate(self._payload(r)) for r in rows]

    def audit(self, event):
        self._put(
            "INSERT INTO audit_events (at, actor, action, subject, detail) VALUES (?, ?, ?, ?, ?)",
            (event.at.isoformat(), event.actor, event.action, event.subject, event.detail),
        )

    def audit_events(self, subject=None):
        cols = "at, actor, action, subject, detail"
        if subject is None:
            rows = self._all(f"SELECT {cols} FROM audit_events ORDER BY id")
        else:
            rows = self._all(
                f"SELECT {cols} FROM audit_events WHERE subject = ? ORDER BY id", (subject,)
            )
        return [AuditEvent.model_validate(dict(r)) for r in rows]


def store_profile() -> tuple[str, str]:
    """The one place the deployment's store is chosen.

    Returns ("postgres", dsn) when DATABASE_URL is set, otherwise
    ("sqlite", path) with the path from SQLITE_PATH or the default
    ./data/cashrecon.db. Switching stores is an env change only.
    """
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return ("postgres", dsn)
    return ("sqlite", os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))


def make_store() -> Store:
    """Postgres when DATABASE_URL is set; the local SQLite file
    otherwise. (Tests and evals construct the in-memory double
    explicitly; it is never selected here.)"""
    kind, target = store_profile()
    if kind == "postgres":
        return PostgresStore(target)
    return SqliteStore(target)
