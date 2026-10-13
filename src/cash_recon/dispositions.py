"""Suspense disposition: the governed end of the suspense lifecycle.

Every other path out of suspense applies cash: the tiers, the
re-match pass, a combination, a resolution the gate chose. A
return releases it. But some cash is never identified — the payer
cannot be found, the trail is cold — and real desks cannot let it
sit open forever: at some point it is written off to
miscellaneous income, or it is moved into an
escheatment / unclaimed-property review state, because money
nobody can attribute may still be owed to someone.

That act is the most dangerous one in this blueprint — it is the
only path that takes cash off the desk's open work without
settling a policy — so it is governed the hardest:

- **Eligibility is code.** An item may be proposed only when it
  is OPEN, wholly unapplied, and aged at or beyond the policy
  threshold (`SUSPENSE_DISPOSITION_MIN_AGE_DAYS`, default 90 —
  deliberately beyond the 30-day stale marker: stale is a prompt
  to work an item; disposition is the desk admitting the work is
  done). An item with a standing undecided resolution docket, an
  item already standing in an undecided disposition pack, and an
  item whose bank line carries an **open return chain** (the bank
  has taken part of the line back and may take more — the line's
  story is not finished) are each refused, in words, naming the
  blocker.
- **The proposal is per item, with a kind and a reason.** A
  disposition pack (`PACK-DISPOSE-<date>`) is assembled by a
  named person; each item on it carries a KIND —
  `WRITE_OFF_INCOME` or `ESCHEAT_REVIEW` — and the assembler's
  written reason for that item. The kinds are destinations, not
  severities: a write-off takes the cash to income; an escheat
  review holds it as owed to someone, on a list that stays
  visible.
- **Approval is dual control, always.** Two approvers, both
  different from the assembler and from each other — not above a
  value threshold, not for large items only: always. This is the
  same shape as the fraud-review class elsewhere in the Trida
  blueprints: a decision one person cannot make alone. The gate
  enforces it (see gate.approve_pack); assembly here only
  proposes.
- **The books keep the term.** Dispositioned cash leaves the
  suspense identity through its own term — the close pack roll
  gains `disposed_from_suspense`, split by kind — the items leave
  suspense aging the day they are decided, the day report for
  the decision day names them, and a dispositioned line's audit
  export foots with the disposition as a term. **Disposition is
  a state, never a deletion**: the item keeps its amount, its
  received date, its reason, and the full chain — assembler,
  reason, both approvers, pack — on its record.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from . import webhooks
from .config import Policy as PolicyConfig
from .models import AuditEvent, DispositionInfo, PostingPack, SuspenseDisposition
from .money import ZERO, as_str
from .store import Store

WRITE_OFF_INCOME = "WRITE_OFF_INCOME"
ESCHEAT_REVIEW = "ESCHEAT_REVIEW"
DISPOSITION_KINDS = (WRITE_OFF_INCOME, ESCHEAT_REVIEW)


class DispositionError(RuntimeError):
    """A disposition act was refused. The message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_disposition_pack(pack: PostingPack) -> bool:
    return pack.pack_id.startswith("PACK-DISPOSE-")


def eligibility_blockers(store: Store, item, as_of: date,
                         policy: PolicyConfig) -> list[str]:
    """Why this item cannot be proposed for disposition today,
    as a list of blockers in words — empty when it can. Every
    check is code; the order is the order a reviewer would ask
    the questions."""
    if item.status != "open":
        return [
            f"suspense item {item.suspense_id} is {item.status}: it holds "
            "no open cash to dispose of"
        ]
    line = next(
        (l for l in store.bank_lines() if l.bank_line_id == item.bank_line_id),
        None,
    )
    if line is None:
        return [
            f"suspense item {item.suspense_id} names bank line "
            f"{item.bank_line_id}, which is not on the books: there is no "
            "cash record to dispose of"
        ]
    blockers: list[str] = []
    # An open return chain: the bank has taken part of this line
    # back and the returns do not yet sum to the line — the rest
    # may still come back, so the line's cash is not the desk's
    # to dispose of yet.
    rets = store.returns_for_line(item.bank_line_id)
    if rets:
        returned = sum((r.amount for r in rets), ZERO)
        if returned < line.amount:
            blockers.append(
                f"bank line {line.bank_line_id} has an open return chain: "
                f"{len(rets)} return(s) on the record total {returned} of "
                f"the line's {line.amount}, and the bank may still take "
                "back the rest — cash whose story is unfinished is not "
                "disposed of"
            )
    # A standing undecided resolution docket: the item already
    # has a governed hypothesis in front of the gate.
    for pack in store.packs():
        if pack.status.value != "proposed":
            continue
        if pack.resolution is not None \
                and pack.resolution.suspense_id == item.suspense_id:
            blockers.append(
                f"suspense item {item.suspense_id} already stands in "
                f"undecided resolution pack {pack.pack_id}: the gate "
                "decides that docket first — reject it, and the item "
                "can be proposed for disposition"
            )
        if pack.disposition is not None and any(
            d.suspense_id == item.suspense_id for d in pack.disposition.items
        ):
            blockers.append(
                f"suspense item {item.suspense_id} already stands in "
                f"undecided disposition pack {pack.pack_id}: one "
                "standing docket per item"
            )
    # Wholly unapplied: no part of the line's cash worked, and no
    # pack at the gate (or decided) speaks for any of it.
    allocated = {
        entry.allocation.bank_line_id: pack.pack_id
        for pack in store.packs() if pack.status.value != "rejected"
        for entry in pack.entries if entry.allocation
    }
    if item.amount != line.amount:
        blockers.append(
            f"suspense item {item.suspense_id} holds {item.amount} of "
            f"bank line {line.bank_line_id}'s {line.amount}: part of "
            "this line's cash has already been worked, so the item is "
            "not wholly unapplied — the remainder is worked through "
            "the re-match pass and the workbench, not disposed of"
        )
    elif line.bank_line_id in allocated:
        blockers.append(
            f"bank line {line.bank_line_id} already stands allocated "
            f"in pack {allocated[line.bank_line_id]}: its cash is "
            "spoken for at the gate, not sitting unapplied"
        )
    age = item.age_days(as_of)
    if age < policy.suspense_disposition_min_age_days:
        blockers.append(
            f"suspense item {item.suspense_id} is {age} day(s) old "
            f"(received {item.received_date}), short of the "
            f"{policy.suspense_disposition_min_age_days}-day disposition "
            "floor (SUSPENSE_DISPOSITION_MIN_AGE_DAYS): cash is "
            "disposed of only when it has aged past the desk's "
            "attempts to identify it"
        )
    return blockers


def assemble_disposition_pack(store: Store, specs: list[dict], actor: str,
                              as_of: date, policy: PolicyConfig) -> PostingPack:
    """Assemble a disposition pack from per-item proposals:
    each spec names a suspense item, a disposition kind, and the
    assembler's written reason for that item. Refused, in words,
    when the act is malformed (no actor, no items, a duplicated
    item, an unknown kind, a missing reason) or when any named
    item is not eligible — with the item and its blocker named.
    The items stay OPEN: the pack proposes; only the gate's dual
    approval disposes."""
    if not actor or not actor.strip():
        raise DispositionError("a disposition pack is assembled by a named person")
    if not specs:
        raise DispositionError("a disposition pack proposes at least one item")
    seen: set[str] = set()
    items: list[SuspenseDisposition] = []
    for spec in specs:
        suspense_id = spec.get("suspense_id")
        kind = spec.get("kind")
        reason = (spec.get("reason") or "").strip()
        if suspense_id in seen:
            raise DispositionError(
                f"suspense item {suspense_id} is proposed twice in one "
                "pack: one disposition per item")
        seen.add(suspense_id)
        if kind not in DISPOSITION_KINDS:
            raise DispositionError(
                f"unknown disposition kind {kind!r} for suspense item "
                f"{suspense_id}; the kinds are: {', '.join(DISPOSITION_KINDS)}")
        if not reason:
            raise DispositionError(
                f"suspense item {suspense_id} carries no reason: every "
                "disposition is proposed with the assembler's written "
                "reason for that item, on the record")
        item = next(
            (i for i in store.suspense_items() if i.suspense_id == suspense_id),
            None,
        )
        if item is None:
            raise DispositionError(f"no such suspense item: {suspense_id}")
        blockers = eligibility_blockers(store, item, as_of, policy)
        if blockers:
            raise DispositionError(blockers[0])
        items.append(SuspenseDisposition(
            suspense_id=item.suspense_id,
            bank_line_id=item.bank_line_id,
            amount=item.amount,
            currency=item.currency,
            kind=kind,
            reason=reason,
        ))
    pack_id = f"PACK-DISPOSE-{as_of.isoformat()}"
    if store.pack(pack_id) is not None:
        n = 2
        while store.pack(f"{pack_id}-{n}") is not None:
            n += 1
        pack_id = f"{pack_id}-{n}"
    pack = PostingPack(
        pack_id=pack_id, value_date=as_of, assembled_by=actor,
        disposition=DispositionInfo(items=items),
    )
    store.save_pack(pack)
    by_kind = {
        kind: sum((d.amount for d in items if d.kind == kind), ZERO)
        for kind in DISPOSITION_KINDS
    }
    store.audit(AuditEvent(
        at=_now(), actor=actor, action="suspense_disposition_pack_assembled",
        subject=pack_id,
        detail=f"{len(items)} suspense disposition(s) proposed: "
               + "; ".join(
                   f"{d.suspense_id} {d.amount} {d.currency} to {d.kind} "
                   f"({d.reason})" for d in items)
               + ". Stops at the gate: approval always takes two "
                 "approvers, neither of them the assembler.",
    ))
    webhooks.emit(store, "pack_assembled", pack_id, actor, {
        "dispositions": str(len(items)),
        "write_off_income": as_str(by_kind[WRITE_OFF_INCOME]),
        "escheat_review": as_str(by_kind[ESCHEAT_REVIEW]),
    }, pack_kind="suspense_disposition")
    return pack


def disposition_candidates(store: Store, as_of: date,
                           policy: PolicyConfig) -> dict:
    """The disposition desk's view: every open suspense item,
    its age, and whether it may be proposed today — with every
    blocker stated in words when it may not. Pure read."""
    rows = []
    for item in sorted(store.suspense_items(), key=lambda i: i.received_date):
        if item.status != "open":
            continue
        blockers = eligibility_blockers(store, item, as_of, policy)
        rows.append({
            "suspense_id": item.suspense_id,
            "bank_line_id": item.bank_line_id,
            "amount": as_str(item.amount),
            "currency": item.currency,
            "received_date": item.received_date.isoformat(),
            "age_days": item.age_days(as_of),
            "reason": item.reason,
            "eligible": not blockers,
            "blockers": blockers,
        })
    return {
        "as_of": as_of.isoformat(),
        "min_age_days": policy.suspense_disposition_min_age_days,
        "items": rows,
    }


def disposition_ledger(store: Store, month: tuple[int, int] | None = None) -> dict:
    """The disposition ledger: every item the gate has disposed
    of, by kind, with the full chain beside the money — and the
    escheat-review items as their own list, because that cash is
    not income: it is owed to someone the desk could not find,
    and it stays visible until it is claimed or the state's
    process takes it. Items standing in proposed packs are listed
    as proposed — visible, but counted in neither kind's totals.
    With `month`, only dispositions decided in it count — the
    form the close pack cites."""
    items: list[dict] = []
    by_kind: dict[str, dict] = {}
    for item in sorted(store.suspense_items(), key=lambda i: i.suspense_id):
        if item.status != "dispositioned":
            continue
        pack = store.pack(item.disposition_pack_id) \
            if item.disposition_pack_id else None
        if month is not None:
            decided = pack.value_date if pack is not None else item.dispositioned_on
            if decided is None or (decided.year, decided.month) != month:
                continue
        row = {
            "suspense_id": item.suspense_id,
            "bank_line_id": item.bank_line_id,
            "amount": as_str(item.amount),
            "currency": item.currency,
            "kind": item.disposition_kind,
            "reason": item.disposition_reason,
            "received_date": item.received_date.isoformat(),
            "dispositioned_on": (
                item.dispositioned_on.isoformat() if item.dispositioned_on else None
            ),
            "pack_id": item.disposition_pack_id,
            "assembled_by": pack.assembled_by if pack else None,
            "approvals": list(pack.approvals) if pack else [],
        }
        items.append(row)
        slot = by_kind.setdefault(item.disposition_kind, {
            "count": 0, "totals_by_currency": {},
        })
        slot["count"] += 1
        ccy = slot["totals_by_currency"]
        ccy[item.currency] = as_str(
            Decimal(ccy.get(item.currency, "0.00")) + item.amount)
    proposed: list[dict] = []
    for pack in sorted(store.packs(), key=lambda p: (p.value_date, p.pack_id)):
        if pack.disposition is None or pack.status.value != "proposed":
            continue
        if month is not None and (pack.value_date.year, pack.value_date.month) != month:
            continue
        for d in pack.disposition.items:
            proposed.append({
                "suspense_id": d.suspense_id,
                "bank_line_id": d.bank_line_id,
                "amount": as_str(d.amount),
                "currency": d.currency,
                "kind": d.kind,
                "reason": d.reason,
                "pack_id": pack.pack_id,
                "status": pack.status.value,
            })
    return {
        "by_kind": {
            kind: by_kind[kind] for kind in DISPOSITION_KINDS if kind in by_kind
        },
        "items": items,
        "escheat_review": [r for r in items if r["kind"] == ESCHEAT_REVIEW],
        "proposed": proposed,
        "note": (
            "WRITE_OFF_INCOME items left suspense as income. "
            "ESCHEAT_REVIEW items are not income: the cash is owed to "
            "someone the desk could not identify, and it stays on "
            "this list — disposition is a state, never a deletion."
        ),
    }


def disposition_decisions(store: Store, day: date) -> dict:
    """The dispositions standing in packs dated `day`, as the day
    report's own section: every proposed item with its kind and
    reason, the pack's state beside it, and the totals that
    actually moved — approved packs only, by kind — because the
    decision day is where a reader looks for what left suspense
    and why."""
    items: list[dict] = []
    totals: dict[str, Decimal] = {kind: ZERO for kind in DISPOSITION_KINDS}
    for pack in sorted(store.packs(on=day), key=lambda p: p.pack_id):
        if pack.disposition is None:
            continue
        for d in pack.disposition.items:
            items.append({
                "suspense_id": d.suspense_id,
                "bank_line_id": d.bank_line_id,
                "amount": as_str(d.amount),
                "currency": d.currency,
                "kind": d.kind,
                "reason": d.reason,
                "pack_id": pack.pack_id,
                "status": pack.status.value,
                "approvals": list(pack.approvals),
            })
            if pack.status.value == "approved":
                totals[d.kind] += d.amount
    return {
        "count": len(items),
        "items": items,
        "decided_totals_by_kind": {
            kind: as_str(total) for kind, total in totals.items() if total > ZERO
        },
    }
