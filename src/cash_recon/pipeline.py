"""The day run, as a LangGraph graph.

    ingest ─▶ screen ─▶ read ─▶ match ─▶ persist ─▶ assemble ─▶ sweep ─▶ report

- **screen**: remittance documents are untrusted — instruction-like
  text is fenced out before any reader sees the document, and a
  near-duplicate advice is flagged (never silently dropped).
- **read**: the reader backend (a real model when configured, the
  labelled offline fallback otherwise) turns advice text into
  proposed allocations, cited to the text.
- **match**: the tiered matcher, in code, decides every bank line.
- **persist**: exceptions, suspense items and audit events are stored.
- **assemble**: the day's posting pack is built for the human gate.
- **sweep**: suspense older than the configured age earns a
  STALE_SUSPENSE exception, so aging is a queue, not a surprise.
- **report**: the daily reconciliation report, footing checked.

The graph stops at the gate. Approval is a separate human act.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from . import documents, gate
from .config import Policy as PolicyConfig
from .dataset import Dataset
from .llm import OfflineReader
from .matcher import MatchContext, match_line
from .models import AuditEvent, ExceptionCode, ExceptionRecord, SuspenseItem
from .money import ZERO
from .objects import ObjectStore
from .report import day_report
from .store import Store

log = logging.getLogger("cash_recon.pipeline")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ingest_dataset(store: Store, objects: ObjectStore, dataset: Dataset) -> None:
    """Load the synthetic world: reference data, bank lines, and the
    remittance documents — documents into object storage, records into
    the store, opening suspense onto the ledger."""
    for broker in dataset.brokers:
        store.upsert_broker(broker)
    for rec in dataset.receivables:
        store.upsert_receivable(rec)
    for line in dataset.bank_lines:
        store.add_bank_line(line)
    for rem in dataset.remittances:
        key = f"remittances/{rem.remittance_id}.txt"
        objects.put(key, rem.text.encode(), "text/plain")
        rem.object_key = key
        store.add_remittance(rem)
    for item in dataset.opening_suspense:
        store.add_suspense(item)
    store.audit(AuditEvent(at=_now(), actor="ingest", action="dataset_loaded", subject="northgate",
                           detail=f"{len(dataset.receivables)} policies, {len(dataset.bank_lines)} bank lines, "
                                  f"{len(dataset.remittances)} remittances"))


class DayState(TypedDict, total=False):
    day: str
    actor: str
    outcomes: list[dict]
    pack_id: str | None
    document_notes: list[str]
    report: dict


def build_graph(store: Store, objects: ObjectStore, reader, policy: PolicyConfig):
    def screen(state: DayState) -> DayState:
        day = date.fromisoformat(state["day"])
        notes: list[str] = []
        known = store.embeddings()
        for rem in store.remittances(on=day):
            visible, flagged, _hits = documents.screen_text(rem.text)
            rem.text = visible
            rem.injection_flagged = flagged
            vector = documents.embed(visible)
            duplicate_of = documents.find_near_duplicate(rem.remittance_id, vector, known)
            store.set_embedding(rem.remittance_id, vector)
            known[rem.remittance_id] = vector
            store.save_remittance(rem)
            if flagged:
                notes.append(f"{rem.remittance_id}: instruction-like text fenced out by the document screen")
                store.audit(AuditEvent(at=_now(), actor="document_screen", action="injection_fenced",
                                       subject=rem.remittance_id,
                                       detail="Instruction-like text inside the remittance was withheld from the reader."))
            if duplicate_of:
                notes.append(f"{rem.remittance_id}: near-duplicate of {duplicate_of}")
                store.audit(AuditEvent(at=_now(), actor="document_screen", action="near_duplicate_flagged",
                                       subject=rem.remittance_id, detail=f"Near-duplicate of {duplicate_of}; flagged for a human, not dropped."))
        return {"document_notes": notes}

    def read(state: DayState) -> DayState:
        day = date.fromisoformat(state["day"])
        for rem in store.remittances(on=day):
            rem.lines = reader.read_remittance(rem.text)
            if rem.stated_total is None:
                rem.stated_total = OfflineReader().stated_total(rem.text)
            store.save_remittance(rem)
            log.info("read remittance %s: %d lines via %s", rem.remittance_id, len(rem.lines), reader.name)
        return {}

    def match(state: DayState) -> DayState:
        day = date.fromisoformat(state["day"])
        lines = sorted(store.bank_lines(on=day), key=lambda l: l.bank_line_id)
        settled = {
            entry.allocation.bank_line_id
            for pack in store.packs()
            if pack.status.value == "approved"
            for entry in pack.entries
            if entry.allocation
        }
        prior = [l for l in store.bank_lines() if (l.value_date, l.bank_line_id) < (day, "~~")]
        ctx = MatchContext(
            receivables={r.policy_number: r for r in store.receivables()},
            brokers={b.broker_id: b for b in store.brokers()},
            remittances=store.remittances(on=day),
            prior_lines=prior,
            settled_line_ids=set(settled),
            proposer=reader,
            policy=policy,
        )
        serialised = []
        for line in lines:
            outcome = match_line(line, ctx)
            if outcome.allocations:
                ctx.settled_line_ids.add(line.bank_line_id)
                ctx.prior_lines.append(line)
            for exc in outcome.exceptions:
                if exc.code == ExceptionCode.NETTING_MISMATCH:
                    broker = ctx.brokers.get(exc.broker_id) if exc.broker_id else None
                    note = reader.draft_broker_note(
                        broker.name if broker else "the broker",
                        exc.policy_number or "",
                        _expected_net(ctx, exc),
                        line.amount,
                    )
                    outcome.notes.append("Broker query note drafted (never sent by the system).")
                    store.audit(AuditEvent(at=_now(), actor=f"reader:{reader.name}", action="broker_note_drafted",
                                           subject=line.bank_line_id, detail=note))
            serialised.append(outcome.model_dump())
            log.info("matched %s: tier=%s allocations=%s exceptions=%s",
                     line.bank_line_id, outcome.tier, outcome.allocated_total,
                     [e.code.value for e in outcome.exceptions])
        return {"outcomes": serialised}

    def persist(state: DayState) -> DayState:
        from .matcher import LineOutcome

        day = date.fromisoformat(state["day"])
        lines = {l.bank_line_id: l for l in store.bank_lines(on=day)}
        for raw in state["outcomes"]:
            outcome = LineOutcome.model_validate(raw)
            line = lines[outcome.bank_line_id]
            for exc in outcome.exceptions:
                store.add_exception(exc)
            if outcome.suspense_amount > ZERO:
                reason = outcome.exceptions[0].code.value if outcome.exceptions else "Unallocated remainder"
                store.add_suspense(SuspenseItem(
                    suspense_id=f"SUS-{line.bank_line_id}", bank_line_id=line.bank_line_id,
                    amount=outcome.suspense_amount, received_date=line.value_date, reason=reason))
            store.audit(AuditEvent(at=_now(), actor="matcher", action="line_matched", subject=line.bank_line_id,
                                   detail=f"tier={outcome.tier}, allocated={outcome.allocated_total}, "
                                          f"suspense={outcome.suspense_amount}, "
                                          f"exceptions={[e.code.value for e in outcome.exceptions]}"))
        return {}

    def assemble(state: DayState) -> DayState:
        from .matcher import LineOutcome

        day = date.fromisoformat(state["day"])
        outcomes = [LineOutcome.model_validate(raw) for raw in state["outcomes"]]
        if not outcomes:
            return {"pack_id": None}
        pack = gate.assemble_pack(day, outcomes, state["actor"], policy)
        store.save_pack(pack)
        store.save_allocations([e.allocation for e in pack.entries if e.allocation], pack.pack_id)
        store.audit(AuditEvent(at=_now(), actor=state["actor"], action="pack_assembled", subject=pack.pack_id,
                               detail=f"{len(pack.entries)} entries, allocations {pack.allocation_total}"))
        return {"pack_id": pack.pack_id}

    def sweep(state: DayState) -> DayState:
        day = date.fromisoformat(state["day"])
        existing = {e.exception_id for e in store.exceptions()}
        for item in store.suspense_items():
            if item.age_days(day) > policy.stale_suspense_days:
                exc_id = f"EXC-{item.suspense_id}-STALE_SUSPENSE"
                if exc_id not in existing:
                    store.add_exception(ExceptionRecord(
                        exception_id=exc_id, code=ExceptionCode.STALE_SUSPENSE,
                        bank_line_id=item.bank_line_id, policy_number=None, broker_id=None,
                        amount=item.amount,
                        detail=f"Unapplied cash of {item.amount} received {item.received_date} is "
                               f"{item.age_days(day)} days old — past the {policy.stale_suspense_days}-day "
                               "threshold. Aged unapplied cash is named on every report until resolved.",
                        first_seen=day))
        return {}

    def report(state: DayState) -> DayState:
        return {"report": day_report(store, date.fromisoformat(state["day"]), policy)}

    graph = StateGraph(DayState)
    for name, fn in [("screen", screen), ("read", read), ("match", match),
                     ("persist", persist), ("assemble", assemble), ("sweep", sweep),
                     ("report", report)]:
        graph.add_node(name, fn)
    graph.set_entry_point("screen")
    for a, b in [("screen", "read"), ("read", "match"), ("match", "persist"),
                 ("persist", "assemble"), ("assemble", "sweep"), ("sweep", "report")]:
        graph.add_edge(a, b)
    graph.add_edge("report", END)
    return graph.compile(checkpointer=_checkpointer())


def _checkpointer():
    """Checkpoints follow the store profile (store.store_profile —
    the same one decision): Postgres saver on the production profile,
    SQLite saver writing into the same local database file on the
    local profile. A checkpointer failure must not stop the desk, so
    any error falls back to memory with a logged warning."""
    from .store import store_profile

    kind, target = store_profile()
    if kind == "postgres":
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver

            saver = PostgresSaver(psycopg.connect(target, autocommit=True))
            saver.setup()
            return saver
        except Exception:
            log.warning("Postgres checkpointer unavailable; using in-memory", exc_info=True)
        return MemorySaver()
    try:
        import sqlite3
        from pathlib import Path

        from langgraph.checkpoint.sqlite import SqliteSaver

        Path(target).parent.mkdir(parents=True, exist_ok=True)
        saver = SqliteSaver(sqlite3.connect(target, check_same_thread=False))
        saver.setup()
        return saver
    except Exception:
        log.warning("SQLite checkpointer unavailable; using in-memory", exc_info=True)
    return MemorySaver()


def _expected_net(ctx: MatchContext, exc: ExceptionRecord):
    rec = ctx.receivables.get(exc.policy_number or "")
    if rec is None:
        return ZERO
    from .matcher import cash_due

    insts = rec.open_installments()
    if not insts:
        return ZERO
    return cash_due(rec, insts[0], ctx.brokers)


def run_day(store: Store, objects: ObjectStore, reader, policy: PolicyConfig,
            day: date, actor: str = "cash-team") -> dict:
    app = build_graph(store, objects, reader, policy)
    result = app.invoke(
        {"day": day.isoformat(), "actor": actor},
        config={"configurable": {"thread_id": f"day-{day.isoformat()}"}},
    )
    return {
        "day": day.isoformat(),
        "pack_id": result.get("pack_id"),
        "document_notes": result.get("document_notes", []),
        "outcomes": result.get("outcomes", []),
        "report": result.get("report"),
    }
