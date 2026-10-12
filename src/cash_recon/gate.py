"""The human gate: posting packs, approvals, write-offs.

Nothing in this blueprint posts itself. The matcher proposes; a pack
is assembled by a named person; a *different* named person approves
it. The rules are code, not convention:

- The assembler of a pack can never approve it (self-approval refused).
- Delta write-offs above the configured single-approver limit need a
  second approver, different from both the first approver and the
  assembler.
- Approval is the only moment receivables change: allocations are
  applied to installments, statuses move, and every applied cent keeps
  its trace — bank line, allocation, pack, approver.
- An approved pack produces the posting file: the CSV a human uploads
  to the accounting system. This system never writes to a GL itself.

Exact T1 matches at or below the policy's auto-apply threshold are
marked auto-appliable on the pack — still listed, still on the day's
report, still auditable. Nothing is hidden by being small.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from .config import Policy as PolicyConfig
from .matcher import LineOutcome
from .models import (
    Allocation,
    AuditEvent,
    InstallmentStatus,
    PolicyStatus,
    PostingEntry,
    PostingPack,
    Tier,
)
from .money import ZERO, cents, net_of


class GateError(RuntimeError):
    """A gate rule refused the act. The message says which rule."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def assemble_pack(day, outcomes: list[LineOutcome], assembled_by: str, policy: PolicyConfig) -> PostingPack:
    pack = PostingPack(pack_id=f"PACK-{day.isoformat()}", value_date=day, assembled_by=assembled_by)
    for outcome in outcomes:
        line_total = outcome.allocated_total + outcome.suspense_amount
        for alloc in outcome.allocations:
            auto = alloc.tier == Tier.T1_EXACT and line_total <= policy.auto_apply_threshold
            pack.entries.append(PostingEntry(kind="allocation", allocation=alloc, auto_applied=auto))
    return pack


def add_write_off(store, pack_id: str, exception_id: str, actor: str) -> PostingPack:
    """Attach a delta write-off (a PARTIAL or NETTING_MISMATCH
    shortfall) to a proposed pack. Deciding it still takes the gate."""
    pack = store.pack(pack_id)
    if pack is None:
        raise GateError(f"no such pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"pack {pack_id} is {pack.status.value}; write-offs attach to proposed packs")
    exc = next((e for e in store.exceptions() if e.exception_id == exception_id), None)
    if exc is None:
        raise GateError(f"no such exception: {exception_id}")
    if exc.code.value not in ("PARTIAL", "NETTING_MISMATCH"):
        raise GateError(f"only PARTIAL or NETTING_MISMATCH deltas can be written off, not {exc.code.value}")
    if exc.status != "open":
        raise GateError(f"exception {exception_id} is {exc.status}")
    if any(e.write_off_exception_id == exception_id for e in pack.entries):
        raise GateError(f"exception {exception_id} is already on pack {pack_id}")
    pack.entries.append(
        PostingEntry(kind="write_off", write_off_policy=exc.policy_number,
                     write_off_amount=exc.amount, write_off_exception_id=exception_id)
    )
    store.save_pack(pack)
    store.audit(AuditEvent(at=_now(), actor=actor, action="write_off_proposed",
                           subject=pack_id, detail=f"{exception_id}: {exc.amount} by {actor}"))
    return pack


def approve_pack(store, pack_id: str, approver: str, policy: PolicyConfig,
                 second_approver: str | None = None) -> PostingPack:
    pack = store.pack(pack_id)
    if pack is None:
        raise GateError(f"no such pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"pack {pack_id} is {pack.status.value}; only a proposed pack can be approved")
    if not approver or approver == pack.assembled_by:
        raise GateError("self-approval refused: the approver must differ from the assembler")
    if pack.write_off_total > policy.write_off_single_limit:
        if not second_approver:
            raise GateError(
                f"write-offs of {pack.write_off_total} exceed the single-approver limit "
                f"{policy.write_off_single_limit}; a second approver is required"
            )
        if second_approver in (approver, pack.assembled_by):
            raise GateError("the second approver must differ from the approver and the assembler")

    pack.approvals = [approver] + ([second_approver] if second_approver else [])
    pack.status = pack.status.APPROVED
    pack.decided_at = _now()
    store.save_pack(pack)

    # The only place receivables change.
    brokers = {b.broker_id: b for b in store.brokers()}
    touched: dict[str, object] = {}
    for entry in pack.entries:
        if entry.kind == "allocation" and entry.allocation:
            alloc = entry.allocation
            rec = touched.get(alloc.policy_number) or store.receivable(alloc.policy_number)
            inst = next(i for i in rec.installments if i.installment_id == alloc.installment_id)
            rate = brokers[rec.broker_id].commission_rate if rec.broker_id else None
            _apply(rec, inst, alloc, rate)
            touched[alloc.policy_number] = rec
        elif entry.kind == "write_off" and entry.write_off_exception_id:
            exc = next(e for e in store.exceptions() if e.exception_id == entry.write_off_exception_id)
            exc.status = "written_off"
            store.update_exception(exc)
    for rec in touched.values():
        if rec.status == PolicyStatus.ACTIVE and rec.open_balance == ZERO:
            rec.status = PolicyStatus.PAID_IN_FULL
        store.upsert_receivable(rec)

    store.audit(AuditEvent(at=_now(), actor=approver, action="pack_approved", subject=pack_id,
                           detail=f"allocations {pack.allocation_total}, write-offs {pack.write_off_total}, "
                                  f"approvals: {', '.join(pack.approvals)}"))
    return pack


def _apply(rec, inst, alloc: Allocation, rate: Decimal | None) -> None:
    """Apply cash to an installment, in gross terms.

    Broker cash settles gross premium net of commission: cash at or
    above the installment's remaining net due settles it in full;
    below that, the gross equivalent of the cash is what was earned.
    """
    remaining_cash_due = inst.open_balance if rate is None else net_of(inst.open_balance, rate)
    if rate is None:
        inst.amount_applied = cents(inst.amount_applied + alloc.amount)
    elif alloc.amount >= remaining_cash_due - Decimal("0.01"):
        inst.amount_applied = inst.amount_due
    else:
        gross_equiv = cents(alloc.amount / (1 - rate))
        inst.amount_applied = cents(inst.amount_applied + gross_equiv)
    if inst.amount_applied >= inst.amount_due:
        inst.amount_applied = inst.amount_due
        inst.status = InstallmentStatus.APPLIED
    elif inst.amount_applied > 0:
        inst.status = InstallmentStatus.PARTIALLY_APPLIED


def reject_pack(store, pack_id: str, actor: str, reason: str) -> PostingPack:
    pack = store.pack(pack_id)
    if pack is None:
        raise GateError(f"no such pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"pack {pack_id} is {pack.status.value}; only a proposed pack can be rejected")
    if not actor or actor == pack.assembled_by:
        raise GateError("rejection must come from someone other than the assembler")
    if not reason.strip():
        raise GateError("a rejection needs a reason on the record")
    pack.status = pack.status.REJECTED
    pack.decided_at = _now()
    store.save_pack(pack)
    store.audit(AuditEvent(at=_now(), actor=actor, action="pack_rejected", subject=pack_id, detail=reason))
    return pack


def posting_file(pack: PostingPack) -> str:
    """The approved pack as a CSV posting file: what a human uploads
    to the accounting system. Rejected/proposed packs produce none."""
    if pack.status.value != "approved":
        raise GateError("a posting file exists only for an approved pack")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["pack_id", "value_date", "kind", "policy_number", "installment_id",
                     "bank_line_id", "amount", "tier", "approved_by"])
    for entry in pack.entries:
        if entry.kind == "allocation" and entry.allocation:
            a = entry.allocation
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "premium_cash",
                             a.policy_number, a.installment_id, a.bank_line_id,
                             f"{a.amount:.2f}", a.tier.value if a.tier else "PROPOSED",
                             ";".join(pack.approvals)])
        else:
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "write_off",
                             entry.write_off_policy or "", "", "",
                             f"{entry.write_off_amount:.2f}", "", ";".join(pack.approvals)])
    return buf.getvalue()
