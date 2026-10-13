"""Returned payments: cash that was applied, then taken back.

An ACH debit bounces, a wire is recalled, a cheque is dishonoured.
The cash the desk already applied is suddenly not there, and the
books must show that honestly — as a chain, not an edit:

    application (the approved pack) ─▶ return (this record) ─▶
    re-application (a NEW bank line, if the payer re-sends)

Recording a return un-applies the line's allocations *exactly*:
every installment it settled re-opens at its prior balance and prior
status. The reversal amounts are not recomputed from the bank line;
they are derived by replaying the approved packs, in approval order,
through the gate's own apply function over pristine receivables —
the same arithmetic that applied the cash, run again, so the delta
attributed to this line is the delta the gate actually posted.

Nothing approved is rewritten. The packs that carried the line keep
their entries byte for byte and gain a RETURN_AFFECTED flag; the
return stands as a reversing entry on the day report of the day it
is recorded and in the month-end close pack. A returned line cannot
be re-applied as-is: if the payer re-sends the money it arrives as a
new bank line, and the duplicate check treats a returned line's
fingerprint as reusable (see matcher.MatchContext.returned_line_ids),
so the re-send is matched on its own merits and linked back to the
returned original on its record.

Full-line returns only. A bank that returns part of a line is
refused here, with the reason stated: model a part-return as a
state and every control in the system would need a second,
fractional notion of "applied". The desk works the difference with
the payer; the system models the whole line coming back.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from . import gate, webhooks
from .models import (
    AuditEvent,
    InstallmentStatus,
    PaymentReturn,
    PolicyStatus,
    ReturnReason,
)
from .money import ZERO
from .store import Store, dump


class ReturnError(RuntimeError):
    """A return recording was refused. The message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def packs_containing(store: Store, bank_line_id: str) -> list:
    """Every pack, any status, carrying an allocation for the line."""
    return [
        pack for pack in store.packs()
        if any(e.allocation and e.allocation.bank_line_id == bank_line_id for e in pack.entries)
    ]


def applied_packs(store: Store, bank_line_id: str) -> list:
    """The approved packs that applied the line — what makes a line
    APPLIED, and therefore returnable."""
    return [p for p in packs_containing(store, bank_line_id) if p.status.value == "approved"]


def is_applied(store: Store, bank_line_id: str) -> bool:
    return bool(applied_packs(store, bank_line_id))


def _replay_deltas(store: Store) -> dict[tuple[str, str], Decimal]:
    """The gross amount each approved allocation actually added to
    its installment, derived by replaying approvals in order through
    gate._apply over pristine receivables. Keyed by
    (bank_line_id, installment_id)."""
    brokers = {b.broker_id: b for b in store.brokers()}
    pristine: dict[str, object] = {}
    for rec in store.receivables():
        clone = rec.model_copy(deep=True)
        for inst in clone.installments:
            inst.amount_applied = ZERO
            inst.status = InstallmentStatus.OPEN
        pristine[rec.policy_number] = clone
    deltas: dict[tuple[str, str], Decimal] = {}
    approved = [p for p in store.packs() if p.status.value == "approved"]
    approved.sort(key=lambda p: (p.decided_at or datetime.min.replace(tzinfo=timezone.utc), p.pack_id))
    for pack in approved:
        for entry in pack.entries:
            if entry.kind != "allocation" or not entry.allocation:
                continue
            alloc = entry.allocation
            rec = pristine.get(alloc.policy_number)
            if rec is None:
                continue
            inst = next(
                (i for i in rec.installments if i.installment_id == alloc.installment_id),
                None,
            )
            if inst is None:
                continue
            before = inst.amount_applied
            rate = brokers[rec.broker_id].rate_on(pack.value_date) if rec.broker_id else None
            gate._apply(rec, inst, alloc, rate)
            key = (alloc.bank_line_id, alloc.installment_id)
            deltas[key] = deltas.get(key, ZERO) + (inst.amount_applied - before)
    return deltas


def record_return(store: Store, bank_line_id: str, reason, actor: str,
                  on_date: date, amount: Decimal | None = None) -> PaymentReturn:
    """Record a bank return against an applied bank line and reverse
    its application exactly. Raises ReturnError, with the reason
    stated, when the return cannot be recorded."""
    try:
        reason = ReturnReason(reason)
    except ValueError:
        valid = ", ".join(r.value for r in ReturnReason)
        raise ReturnError(f"unknown return reason {reason!r}; the bank's reasons are: {valid}") from None
    if not actor or not actor.strip():
        raise ReturnError("a return is recorded by a named person")

    line = next((l for l in store.bank_lines() if l.bank_line_id == bank_line_id), None)
    if line is None:
        raise ReturnError(f"no such bank line: {bank_line_id}")
    if store.return_for_line(bank_line_id) is not None:
        raise ReturnError(f"bank line {bank_line_id} has already been returned")
    packs = applied_packs(store, bank_line_id)
    if not packs:
        raise ReturnError(
            f"bank line {bank_line_id} is not applied: no approved pack carries it, "
            "so there is no application to reverse"
        )
    if amount is not None and amount != line.amount:
        raise ReturnError(
            f"partial returns are not recorded: the bank returned {amount} of "
            f"{bank_line_id}'s {line.amount}, and this system models full-line "
            "returns only. Work the difference with the payer, or record the "
            "return when the whole line comes back."
        )

    # Reverse exactly what the gate applied for this line.
    deltas = _replay_deltas(store)
    line_allocs = [
        entry.allocation
        for pack in packs
        for entry in pack.entries
        if entry.allocation and entry.allocation.bank_line_id == bank_line_id
    ]
    touched: dict[str, object] = {}
    reopened: list[str] = []
    for alloc in line_allocs:
        delta = deltas.get((bank_line_id, alloc.installment_id), ZERO)
        if delta == ZERO:
            continue
        rec = touched.get(alloc.policy_number) or store.receivable(alloc.policy_number)
        if rec is None:
            continue
        inst = next(i for i in rec.installments if i.installment_id == alloc.installment_id)
        inst.amount_applied = inst.amount_applied - delta
        if inst.amount_applied >= inst.amount_due:
            inst.status = InstallmentStatus.APPLIED
        elif inst.amount_applied > ZERO:
            inst.status = InstallmentStatus.PARTIALLY_APPLIED
        else:
            inst.amount_applied = ZERO
            inst.status = InstallmentStatus.OPEN
        touched[alloc.policy_number] = rec
        reopened.append(f"{alloc.policy_number}/{alloc.installment_id} re-opened by {delta}")
    for rec in touched.values():
        if rec.status == PolicyStatus.PAID_IN_FULL and rec.open_balance > ZERO:
            rec.status = PolicyStatus.ACTIVE
        store.upsert_receivable(rec)

    # Cash held in suspense for this line is gone too — it went back
    # to the payer with the rest. The item closes under the return.
    released = ZERO
    for item in store.suspense_items():
        if item.bank_line_id == bank_line_id and item.status == "open":
            item.status = "resolved"
            item.resolved_on = on_date
            store.save_suspense(item)
            released += item.amount

    # The packs are history: flag them, never rewrite them.
    flagged = []
    for pack in packs_containing(store, bank_line_id):
        if "RETURN_AFFECTED" not in pack.flags:
            pack.flags = [*pack.flags, "RETURN_AFFECTED"]
            store.save_pack(pack)
            flagged.append(pack.pack_id)
            store.audit(AuditEvent(
                at=_now(), actor=actor, action="pack_return_affected", subject=pack.pack_id,
                detail=f"Bank line {bank_line_id} was returned ({reason.value}); the pack's "
                       "entries stand as approved — the reversal is the return record."))

    ret = PaymentReturn(
        return_id=f"RET-{bank_line_id}",
        bank_line_id=bank_line_id,
        reason=reason,
        return_date=on_date,
        recorded_by=actor,
        amount=line.amount,
        currency=line.currency,
        pack_ids=[p.pack_id for p in packs],
        detail=(
            f"{reason.value}: the bank returned the full {line.amount} of {bank_line_id} "
            f"(received {line.value_date}, payer {line.payer_name}). "
            + ("; ".join(reopened) if reopened else "No allocations to reverse.")
            + (f" Suspense released: {released}." if released > ZERO else "")
        ),
    )
    store.add_return(ret)
    store.audit(AuditEvent(
        at=_now(), actor=actor, action="payment_returned", subject=bank_line_id,
        detail=ret.detail + f" Packs flagged RETURN_AFFECTED: {', '.join(flagged) or 'none'}."))
    webhooks.emit(store, "return_recorded", bank_line_id, actor, {
        "amount": str(ret.amount),
        "reason": ret.reason.value,
        "return_id": ret.return_id,
    })
    return ret


def line_history(store: Store, bank_line_id: str) -> dict:
    """The line's whole chain, readable in one place: its allocations,
    the packs that carried them (and their decisions), the return if
    one was recorded, and every audit event touching the line or
    those packs — original application, return, and any later
    re-application of a re-sent line all show up here."""
    line = next((l for l in store.bank_lines() if l.bank_line_id == bank_line_id), None)
    packs = packs_containing(store, bank_line_id)
    pack_ids = {p.pack_id for p in packs}
    events = [
        e for e in store.audit_events()
        if e.subject == bank_line_id or e.subject in pack_ids
    ]
    events.sort(key=lambda e: e.at)
    ret = store.return_for_line(bank_line_id)
    resent = [
        l for l in store.bank_lines() if l.resend_of == bank_line_id
    ]
    return {
        "bank_line": json.loads(dump(line)) if line else None,
        "allocations": [json.loads(dump(a)) for a in store.allocations(bank_line_id)],
        "packs": [
            {
                "pack_id": p.pack_id, "status": p.status.value, "flags": list(p.flags),
                "approvals": list(p.approvals), "assembled_by": p.assembled_by,
                "value_date": p.value_date.isoformat(),
            }
            for p in packs
        ],
        "return": json.loads(dump(ret)) if ret else None,
        "resent_as": [l.bank_line_id for l in resent],
        "events": [json.loads(dump(e)) for e in events],
    }
