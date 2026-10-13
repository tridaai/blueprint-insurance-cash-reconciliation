"""The day run, as a LangGraph graph.

    ingest ─▶ screen ─▶ read ─▶ match ─▶ persist ─▶ assemble ─▶ combine ─▶ sweep ─▶ report

- **screen**: remittance documents are untrusted — instruction-like
  text is fenced out before any reader sees the document, and a
  near-duplicate advice is flagged (never silently dropped).
- **read**: the reader backend (a real model when configured, the
  labelled offline fallback otherwise) turns advice text into
  proposed allocations, cited to the text.
- **match**: bank lines that are not premium cash at all are
  classified in code, out of the premium pipeline; the tiered
  matcher, in code, decides every remaining bank line.
- **persist**: exceptions, suspense items and audit events are stored.
- **assemble**: the day's posting pack — and any non-premium
  disposition pack — is built for the human gate.
- **combine**: the combination pass looks for same-payer pairs in
  suspense whose cash together settles one open installment
  exactly, and proposes them in their own pack at the same gate.
- **sweep**: suspense older than the configured age earns a
  STALE_SUSPENSE exception, so aging is a queue, not a surprise.
- **report**: the daily reconciliation report, footing checked.

The graph stops at the gate. Approval is a separate human act.

The run itself is lifecycle-guarded (see `run_day`): a day runs
once, a second run of a run day refuses in words on the audit
trail, and the one sanctioned second run is the recovery of an
attempt that died mid-run — stamped interrupted, its partial work
superseded, its handling state preserved.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from . import combinations, documents, gate, nonpremium, webhooks
from .config import Policy as PolicyConfig
from .dataset import Dataset
from .llm import OfflineReader
from .matcher import MatchContext, match_line
from .models import AuditEvent, DayClaim, ExceptionCode, ExceptionRecord, SuspenseItem
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


def ingest_november_dataset(store: Store, objects: ObjectStore, dataset) -> None:
    """Load November on top of October's books: a broker whose book
    is billed in a second currency, its policies (EUR and USD), and
    its bank lines. No renewals this month; the shape is October's
    so a month is a month, whichever slice a dataset ships."""
    for broker in dataset.brokers:
        store.upsert_broker(broker)
    for rec in dataset.new_receivables:
        store.upsert_receivable(rec)
    for policy_number, renewal in dataset.renewals.items():
        rec = store.receivable(policy_number)
        if rec is None:
            raise ValueError(f"renewal names a policy not on the books: {policy_number}")
        have = {i.installment_id for i in rec.installments}
        fresh = [i for i in renewal["installments"] if i.installment_id not in have]
        if fresh:
            rec.installments = [*rec.installments, *fresh]
            rec.term_start = renewal["term_start"]
            rec.term_end = renewal["term_end"]
            store.upsert_receivable(rec)
    for line in dataset.bank_lines:
        store.add_bank_line(line)
    store.audit(AuditEvent(at=_now(), actor="ingest", action="dataset_loaded", subject="northgate-november",
                           detail=f"November: {len(dataset.new_receivables)} new policies, "
                                  f"{len(dataset.bank_lines)} bank lines"))


def ingest_october_dataset(store: Store, objects: ObjectStore, dataset) -> None:
    """Load October on top of September's books: the broker whose
    terms changed, the new policies, the bank lines — and the
    renewal, which is applied to the policy record already on the
    books so its September applications survive untouched. The
    renewal is idempotent: installments already present are not
    booked twice."""
    for broker in dataset.brokers:
        store.upsert_broker(broker)
    for rec in dataset.new_receivables:
        store.upsert_receivable(rec)
    for policy_number, renewal in dataset.renewals.items():
        rec = store.receivable(policy_number)
        if rec is None:
            raise ValueError(f"renewal names a policy not on the books: {policy_number}")
        have = {i.installment_id for i in rec.installments}
        fresh = [i for i in renewal["installments"] if i.installment_id not in have]
        if fresh:
            rec.installments = [*rec.installments, *fresh]
            rec.term_start = renewal["term_start"]
            rec.term_end = renewal["term_end"]
            store.upsert_receivable(rec)
    for line in dataset.bank_lines:
        store.add_bank_line(line)
    store.audit(AuditEvent(at=_now(), actor="ingest", action="dataset_loaded", subject="northgate-october",
                           detail=f"October: {len(dataset.new_receivables)} new policies, "
                                  f"{len(dataset.bank_lines)} bank lines, renewals for "
                                  f"{', '.join(sorted(dataset.renewals))}"))


def prepare_remittances(store: Store, reader, upto: date) -> int:
    """Screen and read every remittance received on or before `upto`
    that no day run has processed yet — a stored embedding is the
    mark of a screened document. This is how a late advice becomes
    matchable state outside a day run (the re-match pass calls it):
    the same screen, the same reader, the same audit events the day
    graph's screen/read nodes write. Returns how many were prepared.
    """
    known = store.embeddings()
    prepared = 0
    pending = [
        r for r in store.remittances()
        if r.received_date <= upto and r.remittance_id not in known
    ]
    for rem in sorted(pending, key=lambda r: (r.received_date, r.remittance_id)):
        visible, flagged, _hits = documents.screen_text(rem.text)
        rem.text = visible
        rem.injection_flagged = flagged
        vector = documents.embed(visible)
        duplicate_of = documents.find_near_duplicate(rem.remittance_id, vector, known)
        store.set_embedding(rem.remittance_id, vector)
        known[rem.remittance_id] = vector
        if flagged:
            store.audit(AuditEvent(at=_now(), actor="document_screen", action="injection_fenced",
                                   subject=rem.remittance_id,
                                   detail="Instruction-like text inside the remittance was withheld from the reader."))
        if duplicate_of:
            store.audit(AuditEvent(at=_now(), actor="document_screen", action="near_duplicate_flagged",
                                   subject=rem.remittance_id,
                                   detail=f"Near-duplicate of {duplicate_of}; flagged for a human, not dropped."))
        rem.lines = reader.read_remittance(rem.text)
        if rem.stated_total is None:
            rem.stated_total = OfflineReader().stated_total(rem.text)
        store.save_remittance(rem)
        prepared += 1
    return prepared


class DayState(TypedDict, total=False):
    day: str
    actor: str
    outcomes: list[dict]
    classified: list[dict]  # {bank_line_id, kind} — non-premium lines
    pack_id: str | None
    non_premium_pack_id: str | None
    combination_pack_id: str | None
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
        all_lines = sorted(store.bank_lines(on=day), key=lambda l: l.bank_line_id)
        # Non-premium lines are classified in code before the tiers
        # and never reach them: a fee rebate or an own-account
        # transfer has no policy it could settle, and sending it
        # through the matcher would only park it in suspense as if
        # it were unexplained premium cash. The classification is
        # recorded on the audit trail, and the lines ride a
        # disposition pack to the same gate (see assemble).
        lines, classified = [], []
        for line in all_lines:
            kind = nonpremium.classify_line(line)
            if kind is None:
                lines.append(line)
                continue
            classified.append({"bank_line_id": line.bank_line_id, "kind": kind})
            store.audit(AuditEvent(at=_now(), actor="classifier", action="non_premium_classified",
                                   subject=line.bank_line_id,
                                   detail=f"Classified {kind} by the non-premium rules in "
                                          "config: not premium cash, routed to a disposition "
                                          "pack at the gate; never matched, never in suspense."))
            log.info("classified %s as non-premium %s", line.bank_line_id, kind)
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
            returned_line_ids={r.bank_line_id for r in store.returns()},
            payables={p.policy_number: p for p in store.payables(status="open")},
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
        return {"outcomes": serialised, "classified": classified}

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
                    amount=outcome.suspense_amount, received_date=line.value_date, reason=reason,
                    currency=line.currency))
            store.audit(AuditEvent(at=_now(), actor="matcher", action="line_matched", subject=line.bank_line_id,
                                   detail=f"tier={outcome.tier}, allocated={outcome.allocated_total}, "
                                          f"netted={outcome.offset_total}, "
                                          f"suspense={outcome.suspense_amount}, "
                                          f"exceptions={[e.code.value for e in outcome.exceptions]}"))
            if outcome.attempts:
                # The ladder as climbed, persisted with the outcome:
                # the per-line audit export reads these back, so a
                # line shows the tiers attempted, not just the tier
                # that settled.
                store.audit(AuditEvent(at=_now(), actor="matcher", action="line_attempts",
                                       subject=line.bank_line_id,
                                       detail=json.dumps(outcome.attempts)))
            if outcome.resend_of:
                line.resend_of = outcome.resend_of
                store.save_bank_line(line)
                store.audit(AuditEvent(at=_now(), actor="matcher", action="payment_resent",
                                       subject=line.bank_line_id,
                                       detail=f"Re-send of returned line {outcome.resend_of}: "
                                              "the original cash was returned by the bank; "
                                              "this line is linked to it and matched on its own merits."))
        return {}

    def assemble(state: DayState) -> DayState:
        from .matcher import LineOutcome

        day = date.fromisoformat(state["day"])
        result: DayState = {"pack_id": None, "non_premium_pack_id": None}
        outcomes = [LineOutcome.model_validate(raw) for raw in state["outcomes"]]
        if outcomes:
            pack = gate.assemble_pack(day, outcomes, state["actor"], policy)
            store.save_pack(pack)
            store.save_allocations([e.allocation for e in pack.entries if e.allocation], pack.pack_id)
            store.audit(AuditEvent(at=_now(), actor=state["actor"], action="pack_assembled", subject=pack.pack_id,
                                   detail=f"{len(pack.entries)} entries, allocations {pack.allocation_total}"))
            webhooks.emit(store, "pack_assembled", pack.pack_id, state["actor"], {
                "allocations": str(pack.allocation_total),
                "entries": str(len(pack.entries)),
            }, pack_kind="posting")
            result["pack_id"] = pack.pack_id
        classified = state.get("classified") or []
        if classified:
            by_id = {l.bank_line_id: l for l in store.bank_lines(on=day)}
            pairs = [(by_id[c["bank_line_id"]], c["kind"]) for c in classified]
            npack = nonpremium.assemble_disposition_pack(store, day, pairs, state["actor"])
            if npack is not None:
                result["non_premium_pack_id"] = npack.pack_id
        return result

    def combine(state: DayState) -> DayState:
        # The combination pass: after the per-line tiers and the
        # day's packs, look for same-payer pairs whose cash together
        # settles one open installment exactly. Proposals only —
        # they land in a PACK-COMB pack at the same gate.
        day = date.fromisoformat(state["day"])
        summary = combinations.propose_combinations(store, state["actor"], day)
        return {"combination_pack_id": summary["pack_id"]}

    def sweep(state: DayState) -> DayState:
        day = date.fromisoformat(state["day"])
        existing = {e.exception_id for e in store.exceptions()}
        for item in store.suspense_items():
            if item.status != "open":
                continue
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
                     ("persist", persist), ("assemble", assemble), ("combine", combine),
                     ("sweep", sweep), ("report", report)]:
        graph.add_node(name, fn)
    graph.set_entry_point("screen")
    for a, b in [("screen", "read"), ("read", "match"), ("match", "persist"),
                 ("persist", "assemble"), ("assemble", "combine"), ("combine", "sweep"),
                 ("sweep", "report")]:
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
    return cash_due(rec, insts[0], ctx.brokers, exc.first_seen)


class DayRunError(RuntimeError):
    """A day run was refused. The message says why, in words."""


def _run_events(store: Store, day: date) -> list[AuditEvent]:
    """The day's run-lifecycle record: the day_run_* audit events
    whose subject is the day itself, in trail order."""
    return [
        e for e in store.audit_events()
        if e.subject == day.isoformat() and e.action.startswith("day_run_")
    ]


def _claim_reason(store: Store, day: date) -> str:
    """Why a starter that lost the day's claim is refused, naming
    the claim that stands against it: the attempt that holds the
    day, when it was claimed, and by whom."""
    holder = store.day_claim(day)
    if holder is None:
        return (f"day {day.isoformat()} is already claimed by another starter. "
                "A day runs once, in one process at a time.")
    return (f"day {day.isoformat()} is already claimed: attempt {holder.attempt} "
            f"claimed it at {holder.claimed_at.isoformat()} by {holder.actor} "
            f"and the claim stands (state: {holder.state}). A day runs once, "
            "in one process at a time — a second starter is refused, and an "
            "interrupted attempt is recovered only through the recovery path.")


def _refuse_run(store: Store, day: date, actor: str, reason: str) -> None:
    """Record the refusal on the audit trail, then raise it. A
    refused run changes nothing else: the refusal event is the one
    trace it leaves, so 'we tried to run it twice' is itself on the
    record."""
    store.audit(AuditEvent(at=_now(), actor=actor, action="day_run_refused",
                           subject=day.isoformat(), detail=reason))
    raise DayRunError(reason)


def _preserved_handling(store: Store, day: date) -> dict:
    """The handling state a recovery run must not destroy: for the
    day's lines' exceptions, their status, assignee and notes. The
    persist node regenerates an exception record wholesale, and the
    SQL stores' upsert would otherwise reset a resolved exception
    to open and wipe the workbench's assignment and notes — handling
    a crashed attempt's queue between the crash and the recovery is
    legitimate desk work, and the recovery has no business undoing
    it. The regenerated record's code, amount and detail stand; only
    the handling state is carried over."""
    line_ids = {l.bank_line_id for l in store.bank_lines(on=day)}
    return {
        exc.exception_id: (exc.status, exc.assignee, list(exc.handling_notes))
        for exc in store.exceptions()
        if exc.bank_line_id in line_ids
    }


def _restore_handling(store: Store, kept: dict) -> None:
    if not kept:
        return
    current = {e.exception_id: e for e in store.exceptions()}
    for exc_id, (status, assignee, notes) in kept.items():
        exc = current.get(exc_id)
        if exc is None:
            continue
        if (exc.status, exc.assignee, exc.handling_notes) != (status, assignee, notes):
            exc.status, exc.assignee, exc.handling_notes = status, assignee, list(notes)
            store.update_exception(exc)


def run_day(store: Store, objects: ObjectStore, reader, policy: PolicyConfig,
            day: date, actor: str = "cash-team") -> dict:
    """Run one day, once.

    A day run is not idempotent by accident of the stores — left to
    itself a second run overwrites the day's pack in place (an
    approved pack reverts to proposed on the record, a rejection is
    erased), regenerates the day's exceptions over whatever the
    workbench has done with them, and writes the whole attempt into
    the audit trail a second time. So the run guards itself, on the
    audit trail's own run record (day_run_started / day_run_completed
    events, subject = the day):

    - A day whose pack stands DECIDED (approved or rejected) refuses
      a second run, in words, naming the pack and its status. The
      check reads the pack itself, so books written before run
      records existed are guarded too. Rejected work is re-worked
      through the re-match pass, never by re-running the day.
    - A day that ran to completion refuses likewise — its proposed
      pack is waiting at the gate, and a day with no pack (every
      line non-premium, or no lines at all) is still run.
    - The one legitimate second run is a RECOVERY: a prior attempt
      started and never completed (the process died mid-run), or —
      on books that predate run records — a proposed pack stands
      with no completion behind it. The recovery stamps the orphan
      attempt as interrupted on the trail, cleanses the dead
      attempt's allocation-index rows for the day's still-proposed
      packs (the one write that is not naturally overwritten in
      place), preserves the workbench's handling state on the day's
      exceptions across the re-persist, and runs the day fresh on
      its own checkpoint thread. Approval remains the only applier
      throughout: an interrupted run has applied nothing, because
      applying is not the run's to do.

    The trail guard above is read-then-write, and on its own it
    could not make "a day runs once" true across processes: two
    starters reading the same trail at the same moment would both
    pass it. So before any of the run's own writes, the starter
    takes the day's CLAIM at the store (the day_claims record,
    migration 0006): the first starter's insert wins atomically,
    and a second starter — even mid-run of the first — meets the
    standing claim and is refused, naming the attempt that holds
    it. The claim is paired with the day's run LOCK, held for the
    run's duration: it is what lets the sanctioned recovery prove
    the prior attempt is finished — the recovery path reads the
    orphan verdict off the trail as before, then takes the lock
    (a live attempt would still be holding it, and the recovery
    refuses like any other second starter) and takes the claim
    over by compare-and-swap on the interrupted attempt's number.
    A completed run's claim stands as the day's record; a decided
    pack's day is claimed permanently. A starter that claimed the
    day and died before its start reached the trail is detected
    from the claim itself and recovered the same way.
    """
    pack = store.pack(f"PACK-{day.isoformat()}")
    npack = store.pack(f"NPACK-{day.isoformat()}")
    events = _run_events(store, day)
    started = [e for e in events if e.action == "day_run_started"]
    # The run's standing is read off the ORDER of the trail, not the
    # counts: a recovery writes its own started/completed pair on
    # top of the orphan attempt's lone started, so counting would
    # call a recovered day interrupted forever. The day is complete
    # when its last started attempt is followed by a completion, and
    # orphaned when a started attempt is the last word.
    last_started = max(
        (i for i, e in enumerate(events) if e.action == "day_run_started"),
        default=None)
    last_completed = max(
        (i for i, e in enumerate(events) if e.action == "day_run_completed"),
        default=None)
    run_completed = last_completed is not None and (
        last_started is None or last_completed > last_started)
    run_orphaned = last_started is not None and not run_completed

    decided = next(
        (p for p in (pack, npack) if p is not None and p.status.value != "proposed"),
        None,
    )
    if decided is not None:
        _refuse_run(store, day, actor,
                    f"day {day.isoformat()} already ran: pack {decided.pack_id} is "
                    f"{decided.status.value}. A day runs once — the pack stands as "
                    "decided, and re-working the day's cash goes through the "
                    "re-match pass, not a second run.")

    recovery = False
    orphan_note = ""
    if run_completed:
        standing = pack if pack is not None else npack
        if standing is not None:
            _refuse_run(store, day, actor,
                        f"day {day.isoformat()} already ran: pack {standing.pack_id} is "
                        f"{standing.status.value} and waiting at the gate. A day runs "
                        "once — decide the pack there, or reject it and re-work the "
                        "cash through the re-match pass.")
        _refuse_run(store, day, actor,
                    f"day {day.isoformat()} already ran to completion and assembled "
                    "no pack. A day runs once.")
    elif run_orphaned:
        recovery = True
        orphan = started[-1]
        orphan_note = (f"attempt {len(started)} (started {orphan.at.isoformat()} by "
                       f"{orphan.actor}) never completed")
    elif not started and (pack is not None or npack is not None):
        # Books that predate run records: a pack stands for the day
        # with no run behind it on the trail. Treat the attempt as
        # interrupted and recover it the same way — the re-derived
        # pack overwrites the standing one in place, identically,
        # because approval (the only mover of the books it prices
        # from) has not touched them.
        recovery = True
        orphan_note = "a pack stands for the day with no run record behind it"

    if not recovery:
        # A starter that claimed the day and died before its start
        # reached the trail leaves no trail evidence — the claim is
        # the only trace. A claim ahead of the trail's started
        # count, still in the running state, is an interrupted
        # attempt by the claim's own evidence, and is recovered
        # like one (the recovery still has to win the day's lock
        # below before it may take the claim over).
        standing_claim = store.day_claim(day)
        if (
            standing_claim is not None
            and standing_claim.state == "running"
            and standing_claim.attempt > len(started)
        ):
            recovery = True
            orphan_note = (
                f"attempt {standing_claim.attempt} (claimed "
                f"{standing_claim.claimed_at.isoformat()} by "
                f"{standing_claim.actor}) never started")

    attempt = len(started) + 1
    thread_id = f"day-{day.isoformat()}-a{attempt}"
    claim = DayClaim(day=day, attempt=attempt, actor=actor, claimed_at=_now(),
                     state="running", thread_id=thread_id)
    # Win the day before writing anything of the run's own. The
    # refusal paths above (decided pack, completed run) never get
    # here; from here on, exactly one starter holds the day.
    if recovery:
        # The sanctioned recovery, and only it, may take a standing
        # claim over — and only after proving the prior attempt
        # finished: the trail verdict above, plus the day's lock,
        # which a live attempt would still be holding.
        if not store.try_day_lock(day):
            _refuse_run(store, day, actor, _claim_reason(store, day))
        existing = store.day_claim(day)
        if existing is None:
            won = store.claim_day(claim)
        else:
            won = store.reclaim_day(claim, from_attempt=existing.attempt)
        if not won:
            store.release_day_lock(day)
            _refuse_run(store, day, actor, _claim_reason(store, day))
    else:
        if not store.claim_day(claim):
            _refuse_run(store, day, actor, _claim_reason(store, day))
        if not store.try_day_lock(day):
            _refuse_run(store, day, actor, _claim_reason(store, day))

    kept: dict = {}
    try:
        if recovery:
            store.audit(AuditEvent(
                at=_now(), actor=actor, action="day_run_interrupted",
                subject=day.isoformat(),
                detail=f"the day's earlier attempt is marked interrupted: {orphan_note}. "
                       f"Its partial work is superseded by attempt {attempt}; nothing it "
                       "wrote was applied, because approval is the only applier."))
            kept = _preserved_handling(store, day)
            for standing in store.packs(on=day):
                if standing.status.value == "proposed" and (
                    standing.pack_id == f"PACK-{day.isoformat()}"
                    or standing.pack_id.startswith(f"PACK-COMB-{day.isoformat()}")
                ):
                    store.replace_allocations([], standing.pack_id)
        store.audit(AuditEvent(
            at=_now(), actor=actor, action="day_run_started", subject=day.isoformat(),
            detail=f"attempt {attempt} on thread {thread_id}"
                   + ("; recovery of an interrupted attempt" if recovery else "")))

        app = build_graph(store, objects, reader, policy)
        result = app.invoke(
            {"day": day.isoformat(), "actor": actor},
            config={"configurable": {"thread_id": thread_id}},
        )
        if recovery:
            _restore_handling(store, kept)
            # The combination pass leaves a standing PACK-COMB untouched
            # when it finds no new pairs — so a comb pack the dead
            # attempt proposed keeps its entries, but the cleanse above
            # took its index rows. Re-derive the index from the pack as
            # it stands (the index's own contract: it describes the
            # packs), restoring exactly what the proposal wrote.
            for standing in store.packs(on=day):
                if standing.status.value == "proposed" and standing.pack_id.startswith(
                    f"PACK-COMB-{day.isoformat()}"
                ):
                    store.replace_allocations(
                        [e.allocation for e in standing.entries if e.allocation],
                        standing.pack_id,
                    )
        store.audit(AuditEvent(
            at=_now(), actor=actor, action="day_run_completed", subject=day.isoformat(),
            detail=f"attempt {attempt} completed: posting pack "
                   f"{result.get('pack_id') or 'none'}, non-premium pack "
                   f"{result.get('non_premium_pack_id') or 'none'}, combination pack "
                   f"{result.get('combination_pack_id') or 'none'}"))
        claim.state = "completed"
        store.save_day_claim(claim)
    finally:
        store.release_day_lock(day)
    return {
        "day": day.isoformat(),
        "pack_id": result.get("pack_id"),
        "non_premium_pack_id": result.get("non_premium_pack_id"),
        "combination_pack_id": result.get("combination_pack_id"),
        "document_notes": result.get("document_notes", []),
        "outcomes": result.get("outcomes", []),
        "report": result.get("report"),
        "attempt": attempt,
        "recovered": recovery,
    }
