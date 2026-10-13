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

Embeddings carry one contract on every store: `EMBEDDING_DIM`
dimensions (documents.py; the Postgres column is `vector(384)`).
`set_embedding` enforces it in code on all three implementations
and raises `EmbeddingDimensionError` — the JSON-backed stores check
too, so a wrong-dimension vector fails the same deliberate way
everywhere, not only where pgvector happens to reject it.

Day runs carry one contract too: `claim_day` wins a day atomically
(the day_claims table, migration 0006 — insert-or-fail on the day
key), `reclaim_day` takes an interrupted attempt's claim over by
compare-and-swap on (day, attempt, state='running') and on no
other terms, and the per-day run lock (`try_day_lock` /
`release_day_lock`) is held by the attempt that is actually
running — a Postgres advisory lock on a dedicated connection, an
flock on a file beside the SQLite database, a threading lock in
memory — so a starter can tell a live attempt from a dead one:
the lock frees itself when the holder's process dies, while the
claim record stays as the day's standing history.
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

from .documents import EMBEDDING_DIM
from .models import (
    Allocation,
    AuditEvent,
    BankLine,
    Broker,
    Cancellation,
    DayClaim,
    ExceptionRecord,
    PaymentReturn,
    PayoutPack,
    PostingPack,
    Receivable,
    Remittance,
    ReturnPremiumPayable,
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


class EmbeddingDimensionError(ValueError):
    """An embedding was offered at the wrong dimension.

    The store contract is EMBEDDING_DIM dimensions on every profile
    (the production schema's vector(384) — migrations/0002). Raised
    by all three stores before anything is written, so the failure
    is this deliberate message, never a raw driver exception from
    whichever store happened to enforce the schema.
    """


def _checked_embedding(vector: list[float]) -> list[float]:
    vec = [float(v) for v in vector]
    if len(vec) != EMBEDDING_DIM:
        raise EmbeddingDimensionError(
            f"embedding has {len(vec)} dimensions; the store contract is "
            f"{EMBEDDING_DIM} (cash_recon.documents.EMBEDDING_DIM; the "
            f"Postgres schema is vector({EMBEDDING_DIM}), migrations/0002)"
        )
    return vec


class Store(Protocol):
    def upsert_broker(self, broker: Broker) -> None: ...
    def brokers(self) -> list[Broker]: ...
    def upsert_receivable(self, rec: Receivable) -> None: ...
    def receivables(self) -> list[Receivable]: ...
    def receivable(self, policy_number: str) -> Receivable | None: ...
    def add_bank_line(self, line: BankLine) -> None: ...
    def save_bank_line(self, line: BankLine) -> None: ...
    def bank_lines(self, on: date | None = None) -> list[BankLine]: ...
    def add_return(self, ret: PaymentReturn) -> None: ...
    def returns(self, on: date | None = None) -> list[PaymentReturn]: ...
    def return_for_line(self, bank_line_id: str) -> PaymentReturn | None: ...
    def returns_for_line(self, bank_line_id: str) -> list[PaymentReturn]: ...
    def claim_day(self, claim: DayClaim) -> bool: ...
    def day_claim(self, day: date) -> DayClaim | None: ...
    def reclaim_day(self, claim: DayClaim, from_attempt: int) -> bool: ...
    def save_day_claim(self, claim: DayClaim) -> None: ...
    def try_day_lock(self, day: date) -> bool: ...
    def release_day_lock(self, day: date) -> None: ...
    def add_remittance(self, rem: Remittance) -> None: ...
    def save_remittance(self, rem: Remittance) -> None: ...
    def remittances(self, on: date | None = None) -> list[Remittance]: ...
    def remittance_by_hash(self, text_hash: str) -> Remittance | None: ...
    def set_embedding(self, remittance_id: str, vector: list[float]) -> None: ...
    def embeddings(self) -> dict[str, list[float]]: ...
    def save_allocations(self, allocs: list[Allocation], pack_id: str | None) -> None: ...
    def replace_allocations(self, allocs: list[Allocation], pack_id: str) -> None: ...
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
    def add_cancellation(self, cxl: Cancellation) -> None: ...
    def cancellations(self) -> list[Cancellation]: ...
    def cancellation_for(self, policy_number: str) -> Cancellation | None: ...
    def save_payable(self, payable: ReturnPremiumPayable) -> None: ...
    def payable(self, payable_id: str) -> ReturnPremiumPayable | None: ...
    def payables(self, status: str | None = None) -> list[ReturnPremiumPayable]: ...
    def payable_for_policy(self, policy_number: str) -> ReturnPremiumPayable | None: ...
    def save_payout_pack(self, pack: PayoutPack) -> None: ...
    def payout_pack(self, pack_id: str) -> PayoutPack | None: ...
    def payout_packs(self, on: date | None = None) -> list[PayoutPack]: ...
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
        self._allocations: list[tuple[str | None, Allocation]] = []
        self._exceptions: dict[str, ExceptionRecord] = {}
        self._suspense: dict[str, SuspenseItem] = {}
        self._packs: dict[str, PostingPack] = {}
        self._returns: dict[str, PaymentReturn] = {}
        self._cancellations: dict[str, Cancellation] = {}
        self._payables: dict[str, ReturnPremiumPayable] = {}
        self._payout_packs: dict[str, PayoutPack] = {}
        self._audit: list[AuditEvent] = []
        self._day_claims: dict[date, DayClaim] = {}
        self._claim_guard = threading.Lock()
        self._day_locks: dict[date, threading.Lock] = {}

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

    def save_bank_line(self, line):
        self._bank_lines[line.bank_line_id] = copy.deepcopy(line)

    def bank_lines(self, on=None):
        rows = copy.deepcopy(list(self._bank_lines.values()))
        return [r for r in rows if on is None or r.value_date == on]

    def add_return(self, ret):
        self._returns[ret.return_id] = copy.deepcopy(ret)

    def returns(self, on=None):
        rows = sorted(
            copy.deepcopy(list(self._returns.values())),
            key=lambda r: (r.return_date, r.return_id),
        )
        return [r for r in rows if on is None or r.return_date == on]

    def return_for_line(self, bank_line_id):
        rows = self.returns_for_line(bank_line_id)
        return rows[0] if rows else None

    def returns_for_line(self, bank_line_id):
        return [r for r in self.returns() if r.bank_line_id == bank_line_id]

    # -- day claims & run locks -----------------------------------------
    def claim_day(self, claim):
        with self._claim_guard:
            if claim.day in self._day_claims:
                return False
            self._day_claims[claim.day] = copy.deepcopy(claim)
            return True

    def day_claim(self, day):
        with self._claim_guard:
            return copy.deepcopy(self._day_claims.get(day))

    def reclaim_day(self, claim, from_attempt):
        with self._claim_guard:
            existing = self._day_claims.get(claim.day)
            if (
                existing is None
                or existing.attempt != from_attempt
                or existing.state != "running"
            ):
                return False
            self._day_claims[claim.day] = copy.deepcopy(claim)
            return True

    def save_day_claim(self, claim):
        with self._claim_guard:
            self._day_claims[claim.day] = copy.deepcopy(claim)

    def try_day_lock(self, day):
        with self._claim_guard:
            lock = self._day_locks.setdefault(day, threading.Lock())
        return lock.acquire(blocking=False)

    def release_day_lock(self, day):
        with self._claim_guard:
            lock = self._day_locks.get(day)
        if lock is not None and lock.locked():
            lock.release()

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
        self._embeddings[remittance_id] = _checked_embedding(vector)

    def embeddings(self):
        return dict(self._embeddings)

    # -- outcomes -----------------------------------------------------------
    def save_allocations(self, allocs, pack_id):
        # The SQL stores key the index by (bank_line_id, installment_id)
        # and ignore a second save of the same pair — a line's cash for
        # one installment is indexed once, whoever assembles it. The
        # double implements the same contract, so re-saving a pack's
        # allocations cannot double the index here either.
        have = {(a.bank_line_id, a.installment_id) for _p, a in self._allocations}
        for a in allocs:
            key = (a.bank_line_id, a.installment_id)
            if key in have:
                continue
            have.add(key)
            self._allocations.append((pack_id, copy.deepcopy(a)))

    def replace_allocations(self, allocs, pack_id):
        self._allocations = [(p, a) for p, a in self._allocations if p != pack_id]
        self.save_allocations(allocs, pack_id)

    def allocations(self, bank_line_id=None):
        if bank_line_id is None:
            return copy.deepcopy([a for _p, a in self._allocations])
        return copy.deepcopy([a for _p, a in self._allocations if a.bank_line_id == bank_line_id])

    def add_exception(self, exc):
        self._exceptions[exc.exception_id] = copy.deepcopy(exc)

    def exceptions(self, status=None):
        rows = copy.deepcopy(list(self._exceptions.values()))
        return [r for r in rows if status is None or r.status == status]

    def update_exception(self, exc):
        self._exceptions[exc.exception_id] = copy.deepcopy(exc)

    def add_suspense(self, item):
        # Insert-only, as the SQL stores are (ON CONFLICT DO NOTHING):
        # a second add of the same item must not resurrect a resolved
        # record or re-price cash already on the ledger. save_suspense
        # is the upsert.
        if item.suspense_id not in self._suspense:
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

    # -- return premiums -------------------------------------------------
    def add_cancellation(self, cxl):
        self._cancellations[cxl.cancellation_id] = copy.deepcopy(cxl)

    def cancellations(self):
        rows = sorted(
            copy.deepcopy(list(self._cancellations.values())),
            key=lambda c: (c.recorded_on, c.cancellation_id),
        )
        return rows

    def cancellation_for(self, policy_number):
        for cxl in self._cancellations.values():
            if cxl.policy_number == policy_number:
                return copy.deepcopy(cxl)
        return None

    def save_payable(self, payable):
        self._payables[payable.payable_id] = copy.deepcopy(payable)

    def payable(self, payable_id):
        return copy.deepcopy(self._payables.get(payable_id))

    def payables(self, status=None):
        rows = sorted(
            copy.deepcopy(list(self._payables.values())),
            key=lambda p: (p.opened_on, p.payable_id),
        )
        return [r for r in rows if status is None or r.status == status]

    def payable_for_policy(self, policy_number):
        for payable in self._payables.values():
            if payable.policy_number == policy_number:
                return copy.deepcopy(payable)
        return None

    def save_payout_pack(self, pack):
        self._payout_packs[pack.pack_id] = copy.deepcopy(pack)

    def payout_pack(self, pack_id):
        return copy.deepcopy(self._payout_packs.get(pack_id))

    def payout_packs(self, on=None):
        rows = sorted(
            copy.deepcopy(list(self._payout_packs.values())),
            key=lambda p: (p.value_date, p.pack_id),
        )
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
        self._dsn = dsn
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        # The day-run lock lives on its own connection: an advisory
        # lock is session-scoped, so it must not ride the connection
        # the run's ordinary work uses — and when this process dies,
        # the session ends and the lock frees itself.
        self._lock_conn = None
        self._held_day_locks: set[str] = set()
        self._lock_guard = threading.Lock()
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
            "INSERT INTO receivables (policy_number, broker_id, status, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (policy_number) DO UPDATE SET "
            "broker_id = EXCLUDED.broker_id, status = EXCLUDED.status, "
            "currency = EXCLUDED.currency, payload = EXCLUDED.payload",
            (rec.policy_number, rec.broker_id, rec.status.value, rec.currency, dump(rec)),
        )

    def receivables(self):
        return [Receivable.model_validate(r["payload"]) for r in self._all("SELECT payload FROM receivables")]

    def receivable(self, policy_number):
        row = self._one("SELECT payload FROM receivables WHERE policy_number = %s", (policy_number,))
        return Receivable.model_validate(row["payload"]) if row else None

    # -- inputs --------------------------------------------------------------
    def add_bank_line(self, line):
        self._put(
            "INSERT INTO bank_lines (bank_line_id, value_date, amount, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (bank_line_id) DO NOTHING",
            (line.bank_line_id, line.value_date, line.amount, line.currency, dump(line)),
        )

    def save_bank_line(self, line):
        self._put(
            "INSERT INTO bank_lines (bank_line_id, value_date, amount, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (bank_line_id) DO UPDATE SET "
            "value_date = EXCLUDED.value_date, amount = EXCLUDED.amount, "
            "currency = EXCLUDED.currency, payload = EXCLUDED.payload",
            (line.bank_line_id, line.value_date, line.amount, line.currency, dump(line)),
        )

    def bank_lines(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM bank_lines ORDER BY value_date, bank_line_id")
        else:
            rows = self._all(
                "SELECT payload FROM bank_lines WHERE value_date = %s ORDER BY bank_line_id", (on,)
            )
        return [BankLine.model_validate(r["payload"]) for r in rows]

    def add_return(self, ret):
        self._put(
            "INSERT INTO payment_returns (return_id, bank_line_id, return_date, amount, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (return_id) DO NOTHING",
            (ret.return_id, ret.bank_line_id, ret.return_date, ret.amount, ret.currency, dump(ret)),
        )

    def returns(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM payment_returns ORDER BY return_date, return_id")
        else:
            rows = self._all(
                "SELECT payload FROM payment_returns WHERE return_date = %s ORDER BY return_id", (on,)
            )
        return [PaymentReturn.model_validate(r["payload"]) for r in rows]

    def return_for_line(self, bank_line_id):
        rows = self.returns_for_line(bank_line_id)
        return rows[0] if rows else None

    def returns_for_line(self, bank_line_id):
        rows = self._all(
            "SELECT payload FROM payment_returns WHERE bank_line_id = %s "
            "ORDER BY return_date, return_id",
            (bank_line_id,),
        )
        return [PaymentReturn.model_validate(r["payload"]) for r in rows]

    # -- day claims & run locks -----------------------------------------
    def claim_day(self, claim):
        row = self._one(
            "INSERT INTO day_claims (day, attempt, actor, state, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (day) DO NOTHING RETURNING day",
            (claim.day, claim.attempt, claim.actor, claim.state, dump(claim)),
        )
        return row is not None

    def day_claim(self, day):
        row = self._one("SELECT payload FROM day_claims WHERE day = %s", (day,))
        return DayClaim.model_validate(row["payload"]) if row else None

    def reclaim_day(self, claim, from_attempt):
        row = self._one(
            "UPDATE day_claims SET attempt = %s, actor = %s, state = %s, payload = %s "
            "WHERE day = %s AND attempt = %s AND state = 'running' RETURNING day",
            (claim.attempt, claim.actor, claim.state, dump(claim),
             claim.day, from_attempt),
        )
        return row is not None

    def save_day_claim(self, claim):
        self._put(
            "INSERT INTO day_claims (day, attempt, actor, state, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (day) DO UPDATE SET "
            "attempt = EXCLUDED.attempt, actor = EXCLUDED.actor, "
            "state = EXCLUDED.state, payload = EXCLUDED.payload",
            (claim.day, claim.attempt, claim.actor, claim.state, dump(claim)),
        )

    def _advisory_conn(self):
        if self._lock_conn is None or self._lock_conn.closed:
            self._lock_conn = self._psycopg.connect(self._dsn, autocommit=True)
        return self._lock_conn

    def try_day_lock(self, day):
        key = day.isoformat()
        with self._lock_guard:
            if key in self._held_day_locks:
                return False
            row = self._advisory_conn().execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                (f"cashrecon-day-{key}",),
            ).fetchone()
            if row and row[0]:
                self._held_day_locks.add(key)
                return True
            return False

    def release_day_lock(self, day):
        key = day.isoformat()
        with self._lock_guard:
            if key not in self._held_day_locks:
                return
            self._held_day_locks.discard(key)
            try:
                self._advisory_conn().execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (f"cashrecon-day-{key}",),
                )
            except Exception:
                # The unlock is best-effort: if the connection is
                # gone, the session ended and the lock went with it.
                pass

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
        vector = _checked_embedding(vector)
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
                "INSERT INTO allocations (bank_line_id, policy_number, installment_id, amount, tier, pack_id, currency, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (bank_line_id, installment_id) DO NOTHING",
                (a.bank_line_id, a.policy_number, a.installment_id, a.amount, a.tier.value if a.tier else None, pack_id, a.currency, dump(a)),
            )

    def replace_allocations(self, allocs, pack_id):
        self._put("DELETE FROM allocations WHERE pack_id = %s", (pack_id,))
        self.save_allocations(allocs, pack_id)

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
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (suspense_id) DO NOTHING",
            (item.suspense_id, item.bank_line_id, item.received_date, item.currency, dump(item)),
        )

    def save_suspense(self, item):
        self._put(
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, currency, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (suspense_id) DO UPDATE SET payload = EXCLUDED.payload",
            (item.suspense_id, item.bank_line_id, item.received_date, item.currency, dump(item)),
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

    # -- return premiums ---------------------------------------------------
    def add_cancellation(self, cxl):
        self._put(
            "INSERT INTO cancellations (cancellation_id, policy_number, effective_date, recorded_on, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (cancellation_id) DO NOTHING",
            (cxl.cancellation_id, cxl.policy_number, cxl.effective_date, cxl.recorded_on, dump(cxl)),
        )

    def cancellations(self):
        rows = self._all("SELECT payload FROM cancellations ORDER BY recorded_on, cancellation_id")
        return [Cancellation.model_validate(r["payload"]) for r in rows]

    def cancellation_for(self, policy_number):
        row = self._one(
            "SELECT payload FROM cancellations WHERE policy_number = %s", (policy_number,)
        )
        return Cancellation.model_validate(row["payload"]) if row else None

    def save_payable(self, payable):
        self._put(
            "INSERT INTO return_premium_payables (payable_id, policy_number, status, opened_on, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (payable_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload",
            (payable.payable_id, payable.policy_number, payable.status, payable.opened_on, dump(payable)),
        )

    def payable(self, payable_id):
        row = self._one(
            "SELECT payload FROM return_premium_payables WHERE payable_id = %s", (payable_id,)
        )
        return ReturnPremiumPayable.model_validate(row["payload"]) if row else None

    def payables(self, status=None):
        if status is None:
            rows = self._all("SELECT payload FROM return_premium_payables ORDER BY opened_on, payable_id")
        else:
            rows = self._all(
                "SELECT payload FROM return_premium_payables WHERE status = %s ORDER BY opened_on, payable_id",
                (status,),
            )
        return [ReturnPremiumPayable.model_validate(r["payload"]) for r in rows]

    def payable_for_policy(self, policy_number):
        row = self._one(
            "SELECT payload FROM return_premium_payables WHERE policy_number = %s", (policy_number,)
        )
        return ReturnPremiumPayable.model_validate(row["payload"]) if row else None

    def save_payout_pack(self, pack):
        self._put(
            "INSERT INTO payout_packs (pack_id, value_date, status, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (pack_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload",
            (pack.pack_id, pack.value_date, pack.status.value, dump(pack)),
        )

    def payout_pack(self, pack_id):
        row = self._one("SELECT payload FROM payout_packs WHERE pack_id = %s", (pack_id,))
        return PayoutPack.model_validate(row["payload"]) if row else None

    def payout_packs(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM payout_packs ORDER BY value_date, pack_id")
        else:
            rows = self._all(
                "SELECT payload FROM payout_packs WHERE value_date = %s ORDER BY pack_id", (on,)
            )
        return [PayoutPack.model_validate(r["payload"]) for r in rows]

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
        self._day_lock_fds: dict[str, int] = {}
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
            "INSERT INTO receivables (policy_number, broker_id, status, currency, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (policy_number) DO UPDATE SET "
            "broker_id = excluded.broker_id, status = excluded.status, "
            "currency = excluded.currency, payload = excluded.payload",
            (rec.policy_number, rec.broker_id, rec.status.value, rec.currency, dump(rec)),
        )

    def receivables(self):
        return [Receivable.model_validate(self._payload(r)) for r in self._all("SELECT payload FROM receivables")]

    def receivable(self, policy_number):
        row = self._one("SELECT payload FROM receivables WHERE policy_number = ?", (policy_number,))
        return Receivable.model_validate(self._payload(row)) if row else None

    # -- inputs --------------------------------------------------------------
    def add_bank_line(self, line):
        self._put(
            "INSERT OR IGNORE INTO bank_lines (bank_line_id, value_date, amount, currency, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (line.bank_line_id, line.value_date.isoformat(), str(line.amount), line.currency, dump(line)),
        )

    def save_bank_line(self, line):
        self._put(
            "INSERT INTO bank_lines (bank_line_id, value_date, amount, currency, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (bank_line_id) DO UPDATE SET "
            "value_date = excluded.value_date, amount = excluded.amount, "
            "currency = excluded.currency, payload = excluded.payload",
            (line.bank_line_id, line.value_date.isoformat(), str(line.amount), line.currency, dump(line)),
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

    def add_return(self, ret):
        self._put(
            "INSERT OR IGNORE INTO payment_returns (return_id, bank_line_id, return_date, amount, currency, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ret.return_id, ret.bank_line_id, ret.return_date.isoformat(), str(ret.amount), ret.currency, dump(ret)),
        )

    def returns(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM payment_returns ORDER BY return_date, return_id")
        else:
            rows = self._all(
                "SELECT payload FROM payment_returns WHERE return_date = ? ORDER BY return_id",
                (on.isoformat(),),
            )
        return [PaymentReturn.model_validate(self._payload(r)) for r in rows]

    def return_for_line(self, bank_line_id):
        rows = self.returns_for_line(bank_line_id)
        return rows[0] if rows else None

    def returns_for_line(self, bank_line_id):
        rows = self._all(
            "SELECT payload FROM payment_returns WHERE bank_line_id = ? "
            "ORDER BY return_date, return_id",
            (bank_line_id,),
        )
        return [PaymentReturn.model_validate(self._payload(r)) for r in rows]

    # -- day claims & run locks -----------------------------------------
    def claim_day(self, claim):
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO day_claims (day, attempt, actor, state, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (claim.day.isoformat(), claim.attempt, claim.actor,
                 claim.state, dump(claim)),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def day_claim(self, day):
        row = self._one(
            "SELECT payload FROM day_claims WHERE day = ?", (day.isoformat(),)
        )
        return DayClaim.model_validate(self._payload(row)) if row else None

    def reclaim_day(self, claim, from_attempt):
        with self._lock:
            cur = self.conn.execute(
                "UPDATE day_claims SET attempt = ?, actor = ?, state = ?, payload = ? "
                "WHERE day = ? AND attempt = ? AND state = 'running'",
                (claim.attempt, claim.actor, claim.state, dump(claim),
                 claim.day.isoformat(), from_attempt),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def save_day_claim(self, claim):
        self._put(
            "INSERT INTO day_claims (day, attempt, actor, state, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (day) DO UPDATE SET "
            "attempt = excluded.attempt, actor = excluded.actor, "
            "state = excluded.state, payload = excluded.payload",
            (claim.day.isoformat(), claim.attempt, claim.actor,
             claim.state, dump(claim)),
        )

    def _day_lock_path(self, day) -> Path:
        return self.path.parent / f"{self.path.name}.daylock-{day.isoformat()}"

    def try_day_lock(self, day):
        import fcntl

        key = day.isoformat()
        with self._lock:
            if key in self._day_lock_fds:
                return False
            fd = os.open(self._day_lock_path(day), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            self._day_lock_fds[key] = fd
            return True

    def release_day_lock(self, day):
        import fcntl

        key = day.isoformat()
        with self._lock:
            fd = self._day_lock_fds.pop(key, None)
            if fd is None:
                return
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

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
            (remittance_id, json.dumps(_checked_embedding(vector))),
        )

    def embeddings(self):
        rows = self._all("SELECT remittance_id, embedding FROM remittance_embeddings")
        return {r["remittance_id"]: [float(x) for x in json.loads(r["embedding"])] for r in rows}

    # -- outcomes --------------------------------------------------------------
    def save_allocations(self, allocs, pack_id):
        for a in allocs:
            self._put(
                "INSERT OR IGNORE INTO allocations (bank_line_id, policy_number, installment_id, amount, tier, pack_id, currency, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (a.bank_line_id, a.policy_number, a.installment_id, str(a.amount),
                 a.tier.value if a.tier else None, pack_id, a.currency, dump(a)),
            )

    def replace_allocations(self, allocs, pack_id):
        self._put("DELETE FROM allocations WHERE pack_id = ?", (pack_id,))
        self.save_allocations(allocs, pack_id)

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
            "INSERT OR IGNORE INTO suspense_items (suspense_id, bank_line_id, received_date, currency, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (item.suspense_id, item.bank_line_id, item.received_date.isoformat(), item.currency, dump(item)),
        )

    def save_suspense(self, item):
        self._put(
            "INSERT INTO suspense_items (suspense_id, bank_line_id, received_date, currency, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (suspense_id) DO UPDATE SET payload = excluded.payload",
            (item.suspense_id, item.bank_line_id, item.received_date.isoformat(), item.currency, dump(item)),
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

    # -- return premiums ---------------------------------------------------
    def add_cancellation(self, cxl):
        self._put(
            "INSERT OR IGNORE INTO cancellations (cancellation_id, policy_number, effective_date, recorded_on, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (cxl.cancellation_id, cxl.policy_number, cxl.effective_date.isoformat(),
             cxl.recorded_on.isoformat(), dump(cxl)),
        )

    def cancellations(self):
        rows = self._all("SELECT payload FROM cancellations ORDER BY recorded_on, cancellation_id")
        return [Cancellation.model_validate(self._payload(r)) for r in rows]

    def cancellation_for(self, policy_number):
        row = self._one(
            "SELECT payload FROM cancellations WHERE policy_number = ?", (policy_number,)
        )
        return Cancellation.model_validate(self._payload(row)) if row else None

    def save_payable(self, payable):
        self._put(
            "INSERT INTO return_premium_payables (payable_id, policy_number, status, opened_on, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (payable_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload",
            (payable.payable_id, payable.policy_number, payable.status,
             payable.opened_on.isoformat(), dump(payable)),
        )

    def payable(self, payable_id):
        row = self._one(
            "SELECT payload FROM return_premium_payables WHERE payable_id = ?", (payable_id,)
        )
        return ReturnPremiumPayable.model_validate(self._payload(row)) if row else None

    def payables(self, status=None):
        if status is None:
            rows = self._all("SELECT payload FROM return_premium_payables ORDER BY opened_on, payable_id")
        else:
            rows = self._all(
                "SELECT payload FROM return_premium_payables WHERE status = ? ORDER BY opened_on, payable_id",
                (status,),
            )
        return [ReturnPremiumPayable.model_validate(self._payload(r)) for r in rows]

    def payable_for_policy(self, policy_number):
        row = self._one(
            "SELECT payload FROM return_premium_payables WHERE policy_number = ?", (policy_number,)
        )
        return ReturnPremiumPayable.model_validate(self._payload(row)) if row else None

    def save_payout_pack(self, pack):
        self._put(
            "INSERT INTO payout_packs (pack_id, value_date, status, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (pack_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload",
            (pack.pack_id, pack.value_date.isoformat(), pack.status.value, dump(pack)),
        )

    def payout_pack(self, pack_id):
        row = self._one("SELECT payload FROM payout_packs WHERE pack_id = ?", (pack_id,))
        return PayoutPack.model_validate(self._payload(row)) if row else None

    def payout_packs(self, on=None):
        if on is None:
            rows = self._all("SELECT payload FROM payout_packs ORDER BY value_date, pack_id")
        else:
            rows = self._all(
                "SELECT payload FROM payout_packs WHERE value_date = ? ORDER BY pack_id",
                (on.isoformat(),),
            )
        return [PayoutPack.model_validate(self._payload(r)) for r in rows]

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
