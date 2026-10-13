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

A return may be for part of the line — banks claw back what they
claw back. The returned amount unwinds the line's cash in one
pinned order, the desk order: first the suspense the line still
holds (cash that never settled anything, so taking it back
disturbs nothing), then the line's allocations in the REVERSE of
the order they were applied, the most recent application coming
undone first. Each unwound allocation re-opens by its replayed
gross delta — in full when the return takes all of its remaining
cash, pro-rata (to the cent, at the recorded rates) when it takes
part — so a part-return leaves every figure it touches exact, and
the return record carries the unwind per allocation for the next
return of the same line to build on. A line can be returned more
than once, until its returns sum to the line amount; a return that
would take the sum past the line amount is refused here, in
words, and a part-return never disturbs cash netted against a
return-premium payable (the whole standing balance can always be
returned, exactly as a full return always could).

Nothing approved is rewritten. The packs that carried the line keep
their entries byte for byte and gain a RETURN_AFFECTED flag; the
return stands as a reversing entry on the day report of the day it
is recorded and in the month-end close pack. A returned line cannot
be re-applied as-is: if the payer re-sends the money it arrives as a
new bank line, and the duplicate check treats a FULLY returned
line's fingerprint as reusable (see
matcher.MatchContext.returned_line_ids and fully_returned_line_ids
below), so the re-send is matched on its own merits and linked back
to the returned original on its record. A partly returned line's
fingerprint is not reusable — its application still stands in part,
so a same-amount arrival is a duplicate, not a re-send.
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
    ReturnUnwind,
)
from .money import ZERO, cents
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


def fully_returned_line_ids(store: Store) -> set[str]:
    """The lines whose returns sum to the full line amount — the
    set the duplicate check and the re-send link read. Only a
    FULLY returned line's fingerprint is reusable: a partly
    returned line still stands partly applied, so a same-amount
    arrival against it is a duplicate payment, not a re-send."""
    totals: dict[str, Decimal] = {}
    for ret in store.returns():
        totals[ret.bank_line_id] = totals.get(ret.bank_line_id, ZERO) + ret.amount
    amounts = {l.bank_line_id: l.amount for l in store.bank_lines()}
    return {
        line_id for line_id, total in totals.items()
        if amounts.get(line_id) is not None and total >= amounts[line_id]
    }


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
    its application exactly — the whole standing balance when
    `amount` is None, or the stated part of it. A line may be
    returned more than once; its returns may never sum past the
    line amount. Raises ReturnError, with the reason stated, when
    the return cannot be recorded."""
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
    priors = store.returns_for_line(bank_line_id)
    prior_total = sum((r.amount for r in priors), ZERO)
    if prior_total >= line.amount:
        raise ReturnError(
            f"bank line {bank_line_id} has already been returned in full: its "
            f"{len(priors)} return(s) on the record sum to {prior_total} of the "
            f"line's {line.amount}, and there is nothing left for the bank to "
            "take back"
        )
    packs = applied_packs(store, bank_line_id)
    if not packs:
        raise ReturnError(
            f"bank line {bank_line_id} is not applied: no approved pack carries it, "
            "so there is no application to reverse"
        )
    standing = line.amount - prior_total
    if amount is None:
        amount = standing
    if amount <= ZERO:
        raise ReturnError(f"a return of {amount} takes nothing back; the amount must be positive")
    if amount > standing:
        raise ReturnError(
            f"the bank returned {amount} of {bank_line_id}, but only {standing} of "
            f"the line's {line.amount} is still standing"
            + (f" — {prior_total} already came back in {len(priors)} earlier "
               f"return(s)" if priors else "")
            + ". A line's returns can never exceed the cash the line brought "
            "in; work the difference with the payer."
        )
    is_partial = amount < standing

    # Plan the unwind before touching anything, so a refusal above
    # (or below) leaves the books exactly as they stood.
    deltas = _replay_deltas(store)
    # The line's allocations in application order — the replay's
    # own order: approvals by (decided_at, pack_id), entries in
    # pack order — and in the packs' own order, which is the order
    # the return's detail names them in, as it always has.
    approval_order = sorted(
        packs,
        key=lambda p: (p.decided_at or datetime.min.replace(tzinfo=timezone.utc), p.pack_id),
    )
    applied_entries = [
        (pack.pack_id, entry.allocation)
        for pack in approval_order
        for entry in pack.entries
        if entry.kind == "allocation" and entry.allocation
        and entry.allocation.bank_line_id == bank_line_id
    ]
    named_entries = [
        (pack.pack_id, entry.allocation)
        for pack in packs
        for entry in pack.entries
        if entry.allocation and entry.allocation.bank_line_id == bank_line_id
    ]
    # What still stands per allocation, net of the returns already
    # on the record: each prior return's unwind list is the ledger
    # of what it took, so the remainder is exact, never estimated.
    unwound_cash: dict[tuple[str, str], Decimal] = {}
    unwound_gross: dict[tuple[str, str], Decimal] = {}
    for prior in priors:
        for u in prior.unwind:
            key = (u.pack_id, u.installment_id)
            unwound_cash[key] = unwound_cash.get(key, ZERO) + u.cash
            unwound_gross[key] = unwound_gross.get(key, ZERO) + u.gross

    # The unwind order, pinned: suspense the line still holds
    # first (oldest item first), then allocations in the reverse
    # of application order.
    remaining = amount
    released = ZERO
    suspense_moves: list[tuple[object, Decimal]] = []
    open_items = sorted(
        (i for i in store.suspense_items()
         if i.bank_line_id == bank_line_id and i.status == "open"),
        key=lambda i: (i.received_date, i.suspense_id),
    )
    for item in open_items:
        if remaining <= ZERO:
            break
        take = min(remaining, item.amount)
        suspense_moves.append((item, take))
        released += take
        remaining -= take

    unwind: list[ReturnUnwind] = []
    for pack_id, alloc in reversed(applied_entries):
        if remaining <= ZERO:
            break
        key = (pack_id, alloc.installment_id)
        cash_left = alloc.amount - unwound_cash.get(key, ZERO)
        if cash_left <= ZERO:
            continue
        gross_left = deltas.get((bank_line_id, alloc.installment_id), ZERO) \
            - unwound_gross.get(key, ZERO)
        take = min(remaining, cash_left)
        if take == cash_left:
            gross = gross_left  # fully unwound: the exact remaining delta
        else:
            gross = cents(gross_left * take / cash_left)  # pro-rata, to the cent
        if gross < ZERO:
            gross = ZERO
        unwind.append(ReturnUnwind(
            pack_id=pack_id, policy_number=alloc.policy_number,
            installment_id=alloc.installment_id, cash=take, gross=gross))
        remaining -= take
    if remaining > ZERO and is_partial:
        raise ReturnError(
            f"the bank returned {amount} of {bank_line_id}, but {remaining} of "
            "it cannot be unwound as a part-return: that much of the line's "
            "standing cash is netted against a return-premium payable, which "
            "a part-return does not disturb. Return the whole standing "
            f"balance ({standing}) instead, or work the offset with the desk."
        )

    # Apply the plan. Suspense first: a fully-taken item closes
    # under the return, as it always has; a partly-taken item
    # stays open, holding exactly what is left.
    for item, take in suspense_moves:
        if take == item.amount:
            item.status = "resolved"
            item.resolved_on = on_date
        else:
            item.amount = item.amount - take
        store.save_suspense(item)

    # Then the allocations: each re-opens by its unwound gross,
    # and the installment's status re-derives from what stands.
    gross_by_installment = {
        (u.pack_id, u.installment_id): u.gross for u in unwind
    }
    touched: dict[str, object] = {}
    reopened: list[str] = []
    for pack_id, alloc in named_entries:
        gross = gross_by_installment.get((pack_id, alloc.installment_id), ZERO)
        if gross == ZERO:
            continue
        rec = touched.get(alloc.policy_number) or store.receivable(alloc.policy_number)
        if rec is None:
            continue
        inst = next(i for i in rec.installments if i.installment_id == alloc.installment_id)
        inst.amount_applied = inst.amount_applied - gross
        if inst.amount_applied >= inst.amount_due:
            inst.status = InstallmentStatus.APPLIED
        elif inst.amount_applied > ZERO:
            inst.status = InstallmentStatus.PARTIALLY_APPLIED
        else:
            inst.amount_applied = ZERO
            inst.status = InstallmentStatus.OPEN
        touched[alloc.policy_number] = rec
        reopened.append(f"{alloc.policy_number}/{alloc.installment_id} re-opened by {gross}")
    for rec in touched.values():
        if rec.status == PolicyStatus.PAID_IN_FULL and rec.open_balance > ZERO:
            rec.status = PolicyStatus.ACTIVE
        store.upsert_receivable(rec)

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

    reopened_text = "; ".join(reopened) if reopened else "No allocations to reverse."
    suspense_text = f" Suspense released: {released}." if released > ZERO else ""
    if not priors and not is_partial:
        detail = (
            f"{reason.value}: the bank returned the full {line.amount} of {bank_line_id} "
            f"(received {line.value_date}, payer {line.payer_name}). "
            + reopened_text + suspense_text
        )
    elif is_partial:
        detail = (
            f"{reason.value}: the bank returned part of {bank_line_id}: {amount} of "
            f"the line's {line.amount} (received {line.value_date}, payer "
            f"{line.payer_name}). {prior_total + amount} of the line has now come "
            f"back; {standing - amount} still stands. "
            + reopened_text + suspense_text
            + " The rest of the application stands."
        )
    else:
        detail = (
            f"{reason.value}: the bank returned the remaining {amount} of "
            f"{bank_line_id} (received {line.value_date}, payer {line.payer_name}); "
            f"the line is now returned in full — {line.amount} across "
            f"{len(priors) + 1} returns. "
            + reopened_text + suspense_text
        )
    return_number = len(priors) + 1
    ret = PaymentReturn(
        return_id=(f"RET-{bank_line_id}" if return_number == 1
                   else f"RET-{bank_line_id}-{return_number}"),
        bank_line_id=bank_line_id,
        reason=reason,
        return_date=on_date,
        recorded_by=actor,
        amount=amount,
        currency=line.currency,
        pack_ids=[p.pack_id for p in packs],
        detail=detail,
        partial=is_partial,
        cumulative_returned=prior_total + amount,
        suspense_released=released,
        unwind=unwind,
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
    rets = store.returns_for_line(bank_line_id)
    ret = rets[0] if rets else None
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
        "returns": [json.loads(dump(r)) for r in rets],
        "resent_as": [l.bank_line_id for l in resent],
        "events": [json.loads(dump(e)) for e in events],
    }
