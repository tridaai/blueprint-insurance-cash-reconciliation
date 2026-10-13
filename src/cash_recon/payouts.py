"""Payout packs: return premiums leave at the same human gate.

A payable is a promise, not a payment. Settling one takes the
gate's usual discipline, in the gate's usual shape:

- A pack is assembled by a named person from open payables, each
  at the amount standing when the pack was assembled.
- The assembler can never approve their own pack (self-approval
  refused, as everywhere at this gate).
- A pack whose total exceeds the single-approver limit — the same
  `write_off_single_limit` the posting gate uses for write-offs;
  one number for "too much money for one pair of eyes" — needs a
  second approver, different from both.
- Approval re-checks every payable against the pack: if cash was
  netted against one since assembly, its standing amount moved,
  and the pack is refused whole rather than paying a stale figure.
- The approved output is a payout file (CSV) a human hands to the
  bank or the accounting system. This system pays nothing itself.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from . import webhooks
from .config import Policy as PolicyConfig
from .gate import GateError
from .models import AuditEvent, PayableEvent, PayoutEntry, PayoutPack
from .money import ZERO, as_str
from .store import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def assemble_payout_pack(store: Store, day: date, payable_ids: list[str],
                         assembled_by: str) -> PayoutPack:
    """Assemble a proposed payout pack from open payables, each at
    its standing remaining amount. Raises GateError, with the rule
    stated, when a payable cannot go on the pack."""
    if not assembled_by or not assembled_by.strip():
        raise GateError("a payout pack is assembled by a named person")
    if not payable_ids:
        raise GateError("a payout pack names at least one payable")
    if len(set(payable_ids)) != len(payable_ids):
        raise GateError("a payable appears on a payout pack once")

    already = {
        e.payable_id
        for p in store.payout_packs()
        if p.status.value == "proposed"
        for e in p.entries
    }
    entries: list[PayoutEntry] = []
    for payable_id in payable_ids:
        payable = store.payable(payable_id)
        if payable is None:
            raise GateError(f"no such payable: {payable_id}")
        if payable.status != "open":
            raise GateError(
                f"payable {payable_id} is {payable.status}; only an open payable can be paid out"
            )
        if payable.remaining <= ZERO:
            raise GateError(f"payable {payable_id} has nothing standing to pay out")
        if payable_id in already:
            raise GateError(
                f"payable {payable_id} is already on a proposed payout pack; "
                "decide that pack first"
            )
        entries.append(PayoutEntry(
            payable_id=payable.payable_id, policy_number=payable.policy_number,
            payee_kind=payable.payee_kind, payee_name=payable.payee_name,
            broker_id=payable.broker_id, amount=payable.remaining,
        ))

    pack_id = f"PAYOUT-{day.isoformat()}"
    existing = store.payout_pack(pack_id)
    if existing is not None and existing.status.value == "proposed":
        raise GateError(
            f"payout pack {pack_id} is already proposed; decide it before assembling another"
        )
    if existing is not None:
        n = 2
        while store.payout_pack(f"{pack_id}-{n}") is not None:
            n += 1
        pack_id = f"{pack_id}-{n}"
    pack = PayoutPack(pack_id=pack_id, value_date=day,
                      assembled_by=assembled_by, entries=entries)
    store.save_payout_pack(pack)
    store.audit(AuditEvent(
        at=_now(), actor=assembled_by, action="payout_pack_assembled", subject=pack_id,
        detail=f"{len(entries)} payable(s), total {pack.total}; stops at the gate"))
    webhooks.emit(store, "pack_assembled", pack_id, assembled_by, {
        "payouts": as_str(pack.total),
        "payables": str(len(entries)),
    }, pack_kind="payout")
    return pack


def approve_payout_pack(store: Store, pack_id: str, approver: str,
                        policy: PolicyConfig,
                        second_approver: str | None = None) -> PayoutPack:
    """Approve a payout pack and settle its payables. The rules are
    the gate's: a different named approver, a second one above the
    single-approver limit, and every payable still standing at the
    amount the pack was assembled with."""
    pack = store.payout_pack(pack_id)
    if pack is None:
        raise GateError(f"no such payout pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"payout pack {pack_id} is {pack.status.value}; only a proposed pack can be approved")
    if not approver or approver == pack.assembled_by:
        raise GateError("self-approval refused: the approver must differ from the assembler")
    if pack.total > policy.write_off_single_limit:
        if not second_approver:
            raise GateError(
                f"payouts of {pack.total} exceed the single-approver limit "
                f"{policy.write_off_single_limit}; a second approver is required"
            )
        if second_approver in (approver, pack.assembled_by):
            raise GateError("the second approver must differ from the approver and the assembler")

    payables = {}
    for entry in pack.entries:
        payable = store.payable(entry.payable_id)
        if payable is None or payable.status != "open" or payable.remaining != entry.amount:
            standing = "missing" if payable is None else (
                payable.status if payable.status != "open" else str(payable.remaining)
            )
            raise GateError(
                f"payable {entry.payable_id} moved since the pack was assembled: the pack "
                f"says {entry.amount}, the payable stands at {standing}. Reject the pack "
                "and re-assemble it against the payable as it stands."
            )
        payables[entry.payable_id] = payable

    pack.approvals = [approver] + ([second_approver] if second_approver else [])
    pack.status = pack.status.APPROVED
    pack.decided_at = _now()
    store.save_payout_pack(pack)

    for entry in pack.entries:
        payable = payables[entry.payable_id]
        payable.amount_paid = payable.amount_paid + entry.amount
        payable.events = [*payable.events, PayableEvent(
            on=pack.value_date, kind="paid", amount=entry.amount, ref=pack_id,
            note=f"Paid out under {pack_id}, approved by {', '.join(pack.approvals)}.",
        )]
        if payable.remaining == ZERO:
            payable.status = "paid"
        store.save_payable(payable)

    store.audit(AuditEvent(
        at=_now(), actor=approver, action="payout_pack_approved", subject=pack_id,
        detail=f"payouts {pack.total} across {len(pack.entries)} payable(s), "
               f"approvals: {', '.join(pack.approvals)}"))
    webhooks.emit(store, "pack_approved", pack_id, approver, {
        "payouts": as_str(pack.total),
        "payables": str(len(pack.entries)),
    }, pack_kind="payout")
    return pack


def reject_payout_pack(store: Store, pack_id: str, actor: str, reason: str) -> PayoutPack:
    pack = store.payout_pack(pack_id)
    if pack is None:
        raise GateError(f"no such payout pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"payout pack {pack_id} is {pack.status.value}; only a proposed pack can be rejected")
    if not actor or actor == pack.assembled_by:
        raise GateError("rejection must come from someone other than the assembler")
    if not reason.strip():
        raise GateError("a rejection needs a reason on the record")
    pack.status = pack.status.REJECTED
    pack.decided_at = _now()
    store.save_payout_pack(pack)
    store.audit(AuditEvent(at=_now(), actor=actor, action="payout_pack_rejected",
                           subject=pack_id, detail=reason))
    webhooks.emit(store, "pack_rejected", pack_id, actor, {
        "payouts": as_str(pack.total),
        "payables": str(len(pack.entries)),
    }, pack_kind="payout")
    return pack


def payout_file(pack: PayoutPack) -> str:
    """The approved pack as a CSV payout file: what a human hands
    to the bank. Proposed or rejected packs produce none — the
    same rule as the posting file."""
    if pack.status.value != "approved":
        raise GateError("a payout file exists only for an approved payout pack")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["pack_id", "value_date", "payable_id", "policy_number",
                     "payee_kind", "payee_name", "amount", "approved_by"])
    for entry in pack.entries:
        writer.writerow([pack.pack_id, pack.value_date.isoformat(), entry.payable_id,
                         entry.policy_number, entry.payee_kind, entry.payee_name,
                         f"{entry.amount:.2f}", ";".join(pack.approvals)])
    return buf.getvalue()
