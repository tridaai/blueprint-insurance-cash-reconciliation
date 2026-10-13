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

from . import webhooks
from .config import Policy as PolicyConfig
from .matcher import LineOutcome
from .models import (
    Allocation,
    AuditEvent,
    EntryEdit,
    InstallmentStatus,
    PayableEvent,
    PolicyStatus,
    PostingEntry,
    PostingPack,
    Tier,
)
from .money import ZERO, as_str, cents, net_of


class GateError(RuntimeError):
    """A gate rule refused the act. The message says which rule."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_kind(alloc: Allocation) -> str | None:
    """Which model proposal an allocation stands on, if any. The
    remittance tiers are the reader's reading made into allocations;
    a tier-less allocation is the lump-sum proposer's subset. The
    code tiers (T1–T3) are the matcher's own work — no proposal,
    nothing to grade."""
    if alloc.tier == Tier.T4_REMITTANCE:
        return "remittance_read"
    if alloc.tier is None:
        return "lump_sum"
    return None


def assemble_pack(day, outcomes: list[LineOutcome], assembled_by: str, policy: PolicyConfig) -> PostingPack:
    pack = PostingPack(pack_id=f"PACK-{day.isoformat()}", value_date=day, assembled_by=assembled_by)
    for outcome in outcomes:
        line_total = outcome.allocated_total + outcome.suspense_amount
        for alloc in outcome.allocations:
            auto = alloc.tier == Tier.T1_EXACT and line_total <= policy.auto_apply_threshold
            kind = _proposal_kind(alloc)
            pack.entries.append(PostingEntry(
                kind="allocation", allocation=alloc, auto_applied=auto,
                proposal_kind=kind,
                proposed_by=outcome.proposed_by if kind else None,
            ))
        for offset in outcome.offsets:
            pack.entries.append(PostingEntry(kind="payable_offset", payable_offset=offset))
    return pack


def edit_pack_entries(store, pack_id: str, actor: str, adjustments: list[dict]) -> PostingPack:
    """Edit a PROPOSED pack's allocation entries at the gate, before
    approval — the approver's correction of a proposal, on the
    record. Each adjustment names an entry (by its index in the
    pack) and may change its amount and/or its target installment
    (and policy). The rules are the gate's usual kind:

    - the pack must still be proposed; decided packs are history;
    - the editor must be named and must not be the assembler —
      editing is an approver-side act, and the editor is recorded
      on every changed entry, beside the original it replaced;
    - the new target must be a real open installment on the books;
    - after all adjustments, the allocations for each bank line in
      the pack must still sum to what they summed to before — an
      edit redistributes a line's cash, it does not create or
      destroy any; an edit that breaks a line's footing is refused
      whole, with the line and both totals stated.

    The store's allocation index follows the pack, so reports keep
    describing the pack as it stands."""
    pack = store.pack(pack_id)
    if pack is None:
        raise GateError(f"no such pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"pack {pack_id} is {pack.status.value}; only a proposed pack can be edited")
    if not actor or actor == pack.assembled_by:
        raise GateError("pack entries are edited by a named approver-side person, not the assembler")
    if any(e.combination_id for e in pack.entries):
        raise GateError(
            "a combination settlement is edited by nobody: its legs settle "
            "exactly as proposed, together, or the pack is rejected and the "
            "pair proposed again against the books as they then stand"
        )
    if not adjustments:
        raise GateError("an edit names at least one adjustment")

    def line_totals(entries) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for e in entries:
            if e.kind == "allocation" and e.allocation:
                totals[e.allocation.bank_line_id] = (
                    totals.get(e.allocation.bank_line_id, ZERO) + e.allocation.amount
                )
        return totals

    before_totals = line_totals(pack.entries)
    changes: list[str] = []
    for adj in adjustments:
        index = adj.get("entry_index")
        if not isinstance(index, int) or not (0 <= index < len(pack.entries)):
            raise GateError(f"adjustment names entry {index!r}; the pack has {len(pack.entries)} entries")
        entry = pack.entries[index]
        if entry.kind != "allocation" or not entry.allocation:
            raise GateError(f"entry {index} of {pack_id} is not an allocation entry")
        alloc = entry.allocation
        new_policy = adj.get("new_policy") or alloc.policy_number
        new_installment = adj.get("new_installment") or alloc.installment_id
        new_amount = cents(adj["new_amount"]) if adj.get("new_amount") is not None else alloc.amount
        if new_amount <= ZERO:
            raise GateError(f"entry {index}: an allocation amount must be positive, not {new_amount}")
        if (new_policy, new_installment, new_amount) == (alloc.policy_number, alloc.installment_id, alloc.amount):
            continue  # names the entry but changes nothing — not an edit
        rec = store.receivable(new_policy)
        if rec is None:
            raise GateError(f"entry {index}: no policy {new_policy} on the books")
        inst = next((i for i in rec.installments if i.installment_id == new_installment), None)
        if inst is None:
            raise GateError(f"entry {index}: policy {new_policy} has no installment {new_installment}")
        if inst.open_balance <= ZERO and new_installment != alloc.installment_id:
            raise GateError(
                f"entry {index}: installment {new_installment} of {new_policy} is already settled; "
                "an edit cannot target settled work"
            )
        entry.edits.append(EntryEdit(
            at=_now(), by=actor,
            before_policy=alloc.policy_number, before_installment=alloc.installment_id,
            before_amount=alloc.amount,
            after_policy=new_policy, after_installment=new_installment, after_amount=new_amount,
        ))
        changes.append(
            f"entry {index}: {alloc.policy_number}/{alloc.installment_id} {alloc.amount} "
            f"→ {new_policy}/{new_installment} {new_amount}"
        )
        alloc.policy_number, alloc.installment_id, alloc.amount = new_policy, new_installment, new_amount

    if not changes:
        raise GateError("the edit changes nothing: every adjustment restates its entry")

    after_totals = line_totals(pack.entries)
    for line_id, before in before_totals.items():
        after = after_totals.get(line_id, ZERO)
        if after != before:
            raise GateError(
                f"the edit breaks the footing for bank line {line_id}: its allocations "
                f"were {before} and would become {after}. Adjust the line's entries so "
                "they still sum to the same total, or reject the pack instead."
            )
    store.save_pack(pack)
    store.replace_allocations([e.allocation for e in pack.entries if e.allocation], pack_id)
    store.audit(AuditEvent(at=_now(), actor=actor, action="pack_entry_edited", subject=pack_id,
                           detail="; ".join(changes)))
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

    # Combination settlements are re-verified before anything is
    # written, with the payout packs' discipline: the pair was
    # proven against the books at proposal, and if the target
    # installment has moved since — another pack settled part of
    # it — the pack is refused whole rather than applying a stale
    # proof. Each leg is priced at the rate in force on its own
    # value date, exactly as the proposal priced it.
    comb_groups: dict[str, list] = {}
    for entry in pack.entries:
        if entry.combination_id and entry.allocation:
            comb_groups.setdefault(entry.combination_id, []).append(entry.allocation)
    line_dates = {l.bank_line_id: l.value_date for l in store.bank_lines()}
    comb_brokers = {b.broker_id: b for b in store.brokers()}
    comb_rates: dict[tuple[str, str], object] = {}
    for comb_id, allocs in comb_groups.items():
        targets = {(a.policy_number, a.installment_id) for a in allocs}
        if len(allocs) != 2 or len(targets) != 1:
            raise GateError(
                f"combination {comb_id} does not stand as two legs on one "
                "installment; it cannot be approved"
            )
        (policy_number, installment_id), = targets
        rec = store.receivable(policy_number)
        inst = next(
            (i for i in rec.installments if i.installment_id == installment_id), None
        ) if rec is not None else None
        if inst is None:
            raise GateError(
                f"combination {comb_id} names {installment_id}, which is no "
                "longer on the books"
            )
        broker = comb_brokers.get(rec.broker_id) if rec.broker_id else None
        proved = ZERO
        for alloc in allocs:
            if broker is None:
                gross = alloc.amount
                comb_rates[(comb_id, alloc.bank_line_id)] = None
            else:
                rate = broker.rate_on(line_dates[alloc.bank_line_id])
                gross = cents(alloc.amount / (1 - rate))
                comb_rates[(comb_id, alloc.bank_line_id)] = rate
            proved += gross
        if proved != inst.open_balance:
            raise GateError(
                f"combination {comb_id} no longer settles {installment_id}: "
                f"its legs price to {proved} against the installment's "
                f"standing open balance {inst.open_balance}; the books moved "
                "since the proposal — the pack is refused whole"
            )

    # Payable offsets are validated before anything is written: a
    # payable that moved since the pack was assembled refuses the
    # approval whole, with the pack still proposed and nothing
    # applied — the same discipline as a payout pack.
    offset_totals: dict[str, Decimal] = {}
    for entry in pack.entries:
        if entry.kind == "payable_offset" and entry.payable_offset:
            o = entry.payable_offset
            offset_totals[o.payable_id] = offset_totals.get(o.payable_id, ZERO) + o.amount
    for payable_id, total in offset_totals.items():
        payable = store.payable(payable_id)
        if payable is None:
            raise GateError(
                f"pack {pack_id} nets {total} against payable {payable_id}, "
                "which is no longer on the books"
            )
        if total > payable.remaining:
            raise GateError(
                f"pack {pack_id} nets {total} against payable {payable_id}, which has "
                f"only {payable.remaining} standing; the payable moved since the pack "
                "was assembled"
            )

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
            # The settlement rate is the one in force on the pack's
            # value date — the day the cash was worked — not whatever
            # the broker's rate has become by approval day. The one
            # exception is a combination leg: the pair was proven
            # with each leg priced at the rate in force on the day
            # its own cash arrived (the legs can straddle a rate
            # change), so the leg applies at that rate, the price it
            # was proposed at.
            if entry.combination_id:
                rate = comb_rates.get((entry.combination_id, alloc.bank_line_id))
            else:
                rate = brokers[rec.broker_id].rate_on(pack.value_date) if rec.broker_id else None
            _apply(rec, inst, alloc, rate)
            touched[alloc.policy_number] = rec
        elif entry.kind == "write_off" and entry.write_off_exception_id:
            exc = next(e for e in store.exceptions() if e.exception_id == entry.write_off_exception_id)
            exc.status = "written_off"
            store.update_exception(exc)
        elif entry.kind == "payable_offset" and entry.payable_offset:
            offset = entry.payable_offset
            payable = store.payable(offset.payable_id)
            payable.amount_netted = cents(payable.amount_netted + offset.amount)
            payable.events = [*payable.events, PayableEvent(
                on=pack.value_date, kind="netted", amount=offset.amount,
                ref=offset.bank_line_id,
                note=f"Netted under {pack_id}: cash from {offset.bank_line_id} for "
                     f"cancelled policy {offset.policy_number}.",
            )]
            if payable.remaining == ZERO:
                payable.status = "settled"
            store.save_payable(payable)
    for rec in touched.values():
        if rec.status == PolicyStatus.ACTIVE and rec.open_balance == ZERO:
            rec.status = PolicyStatus.PAID_IN_FULL
        store.upsert_receivable(rec)

    # A combination's legs carried open exceptions (UNIDENTIFIED,
    # typically) from their per-line workings. The settlement the
    # gate just approved disposes of them: the cash is applied, and
    # the exceptions close with it, as a re-match recovery closes
    # its line's exceptions.
    if comb_groups:
        leg_line_ids = {
            a.bank_line_id for allocs in comb_groups.values() for a in allocs
        }
        for exc in store.exceptions(status="open"):
            if exc.bank_line_id in leg_line_ids:
                exc.status = "resolved"
                store.update_exception(exc)

    store.audit(AuditEvent(at=_now(), actor=approver, action="pack_approved", subject=pack_id,
                           detail=f"allocations {pack.allocation_total}, write-offs {pack.write_off_total}, "
                                  f"payable offsets {pack.offset_total}, "
                                  f"approvals: {', '.join(pack.approvals)}"))
    webhooks.emit(store, "pack_approved", pack_id, approver, {
        "allocations": as_str(pack.allocation_total),
        "write_offs": as_str(pack.write_off_total),
        "payable_offsets": as_str(pack.offset_total),
        "non_premium": as_str(pack.non_premium_total),
    }, pack_kind="posting")
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
    # A combination's legs were held from suspense at proposal.
    # Rejection frees them: their items re-open with the original
    # received dates (the aging clock never stopped being true),
    # and the pair can be proposed again when the books change.
    comb_line_ids = {
        e.allocation.bank_line_id for e in pack.entries
        if e.combination_id and e.allocation
    }
    if comb_line_ids:
        for item in store.suspense_items():
            if item.bank_line_id in comb_line_ids and item.status == "resolved":
                item.status = "open"
                item.resolved_on = None
                store.save_suspense(item)
    store.audit(AuditEvent(at=_now(), actor=actor, action="pack_rejected", subject=pack_id, detail=reason))
    webhooks.emit(store, "pack_rejected", pack_id, actor, {
        "allocations": as_str(pack.allocation_total),
        "write_offs": as_str(pack.write_off_total),
        "payable_offsets": as_str(pack.offset_total),
        "non_premium": as_str(pack.non_premium_total),
    }, pack_kind="posting")
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
        elif entry.kind == "non_premium" and entry.non_premium:
            np = entry.non_premium
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "non_premium",
                             "", np.kind, np.bank_line_id,
                             f"{np.amount:.2f}", "",
                             ";".join(pack.approvals)])
        elif entry.kind == "payable_offset" and entry.payable_offset:
            o = entry.payable_offset
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "return_premium_offset",
                             o.policy_number, o.payable_id, o.bank_line_id,
                             f"{o.amount:.2f}", "", ";".join(pack.approvals)])
        else:
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "write_off",
                             entry.write_off_policy or "", "", "",
                             f"{entry.write_off_amount:.2f}", "", ";".join(pack.approvals)])
    return buf.getvalue()
