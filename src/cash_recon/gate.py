"""The human gate: posting packs, approvals, write-offs.

Nothing in this blueprint posts itself. The matcher proposes; a pack
is assembled by a named person; a *different* named person approves
it. The rules are code, not convention:

- The assembler of a pack can never approve it (self-approval refused).
- Delta write-offs above the configured single-approver limit need a
  second approver, different from both the first approver and the
  assembler.
- A suspense disposition pack (PACK-DISPOSE-*) is dual control
  ALWAYS: two approvers, neither of them the assembler, whatever
  the amounts — disposing of unidentified cash is a decision one
  person never makes alone.
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
    if pack.resolution is not None:
        raise GateError(
            "a resolution pack's alternates are edited by nobody: the "
            "approver chooses one candidate as scanned, or the pack is "
            "rejected and the item scanned again against the books as "
            "they then stand"
        )
    if pack.disposition is not None:
        raise GateError(
            "a disposition pack's items are edited by nobody: the two "
            "approvers decide the items as proposed — kind, amount and "
            "reason — or the pack is rejected and the dispositions "
            "proposed again"
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
        if alloc.fx_rate is not None:
            # The edit moves cash, not currencies: the allocation
            # keeps the conversion rate recorded when it was created
            # (the rate in force on its bank line's value date), and
            # the functional amount follows the edited cash at that
            # same recorded rate — never at today's.
            alloc.functional_amount = cents(alloc.amount * alloc.fx_rate)

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
    if pack.resolution is not None:
        raise GateError(
            "write-offs do not attach to a resolution pack: it decides what "
            "unidentified cash is, not what a shortfall costs")
    if pack.disposition is not None:
        raise GateError(
            "write-offs do not attach to a disposition pack: it decides "
            "where stale suspense cash goes, not what a shortfall costs")
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
                 second_approver: str | None = None,
                 choice: str | None = None) -> PostingPack:
    """The human gate. Self-approval is refused; write-offs above
    the threshold need a second, different approver. Only an
    approval writes receivables — the apply loop below is the
    single place in the system where cash application happens.

    A resolution pack is decided differently: it carries
    alternates, not entries, and approval must name the chosen
    candidate (`choice`). The gate re-verifies the chosen
    candidate against the books as they now stand, materialises
    exactly that candidate's allocations as the pack's entries,
    and applies them like any other pack's — so the posting file,
    the day totals, and the returns replay all see the one
    settlement that happened, never the sum of the roads not
    taken. A choice on a pack that carries no alternates, or no
    choice on a resolution pack, is refused: the choice is the
    decision.

    A disposition pack is decided under dual control, always:
    a second approver, different from the approver and from the
    assembler, whatever the amounts. Approval re-verifies every
    item still stands open at its proposed amount, then moves
    the items into their disposition state and closes their
    lines' open exceptions; nothing is applied to a receivable,
    because a disposition settles no policy — it ends the
    suspense, on the record."""
    pack = store.pack(pack_id)
    if pack is None:
        raise GateError(f"no such pack: {pack_id}")
    if pack.status.value != "proposed":
        raise GateError(f"pack {pack_id} is {pack.status.value}; only a proposed pack can be approved")
    if not approver or approver == pack.assembled_by:
        raise GateError("self-approval refused: the approver must differ from the assembler")
    if pack.disposition is not None:
        # Dual control, always: a disposition takes cash off the
        # desk's open work without settling a policy, so no single
        # person — and never the assembler, in either chair — can
        # approve one, whatever the amounts. This is not the
        # write-off rule below (a threshold); it is a class rule.
        if not second_approver:
            raise GateError(
                f"pack {pack_id} is a suspense disposition pack: "
                "disposing of unidentified cash is a dual-control act "
                "and always needs a second approver, whatever the "
                "amounts — one person never disposes of cash alone"
            )
        if second_approver == approver:
            raise GateError(
                "the second approver of a disposition must differ "
                "from the approver: two people, two decisions"
            )
        if second_approver == pack.assembled_by:
            raise GateError(
                "the assembler of a disposition pack can be neither "
                "approver: the second approver must differ from the "
                "assembler as well as from the approver"
            )
    if pack.write_off_total > policy.write_off_single_limit:
        if not second_approver:
            raise GateError(
                f"write-offs of {pack.write_off_total} exceed the single-approver limit "
                f"{policy.write_off_single_limit}; a second approver is required"
            )
        if second_approver in (approver, pack.assembled_by):
            raise GateError("the second approver must differ from the approver and the assembler")

    # Resolution packs: the choice is validated before anything is
    # written, with the combination discipline — the candidate was
    # proven against the books at scan time, and if its target
    # installments have moved since, the pack is refused whole
    # rather than applying a stale reading.
    chosen = None
    if pack.resolution is not None:
        info = pack.resolution
        decidable = [c.candidate_id for c in info.candidates if not c.flags]
        if choice is None:
            raise GateError(
                f"pack {pack_id} is a resolution pack for suspense item "
                f"{info.suspense_id}: it carries alternates "
                f"({', '.join(decidable) or 'none decidable'}) and approval "
                "must name the chosen candidate — the gate decides which "
                "reading of this cash stands")
        chosen = next((c for c in info.candidates if c.candidate_id == choice), None)
        if chosen is None:
            raise GateError(
                f"pack {pack_id} carries no candidate {choice!r}; its "
                f"candidates are {', '.join(c.candidate_id for c in info.candidates)}")
        if chosen.flags:
            raise GateError(
                f"candidate {choice} names {chosen.policy_number}, which is "
                f"flagged {', '.join(chosen.flags)}: it stands on the record "
                "as considered, and the gate cannot choose it")
        res_line = next((l for l in store.bank_lines()
                         if l.bank_line_id == info.bank_line_id), None)
        line_date = res_line.value_date if res_line is not None else None
        brokers_now = {b.broker_id: b for b in store.brokers()}
        for spec in chosen.allocations:
            rec = store.receivable(spec.policy_number)
            inst = next((i for i in rec.installments
                         if i.installment_id == spec.installment_id), None) \
                if rec is not None else None
            # What the candidate's cash would settle, priced as the
            # application will price it (the policy's broker rate
            # in force on the day the cash arrived): if that no
            # longer fits the installment's standing balance, the
            # reading the approver is looking at is stale.
            gross = spec.amount
            if rec is not None and rec.broker_id and line_date is not None:
                rate = brokers_now[rec.broker_id].rate_on(line_date)
                gross = cents(spec.amount / (1 - rate))
            if inst is None or gross > inst.open_balance:
                raise GateError(
                    f"candidate {choice} no longer fits the books: "
                    f"{spec.installment_id} of {spec.policy_number} stands "
                    f"open at {inst.open_balance if inst else 'nothing'} "
                    f"against the {gross} the candidate's cash would settle; "
                    "the books moved since the scan — the pack is refused "
                    "whole; reject it and scan the item again")
    elif choice is not None:
        raise GateError(
            f"pack {pack_id} carries no alternates; a choice names nothing here")

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

    # A disposition pack is re-verified before anything is
    # written, with the same discipline: each item was eligible
    # when proposed, and if the cash has since been worked — the
    # item resolved, or its amount moved — the proposal the two
    # approvers are looking at is stale, and the pack is refused
    # whole rather than disposing of cash that is no longer there.
    if pack.disposition is not None:
        items_now = {i.suspense_id: i for i in store.suspense_items()}
        for d in pack.disposition.items:
            item_now = items_now.get(d.suspense_id)
            if item_now is None or item_now.status != "open" \
                    or item_now.amount != d.amount:
                raise GateError(
                    f"disposition of {d.suspense_id} no longer stands: "
                    f"the item is "
                    f"{item_now.status if item_now else 'gone'} at "
                    f"{item_now.amount if item_now else 'nothing'} "
                    f"against the {d.amount} proposed; the books moved "
                    "since the pack was assembled — the pack is refused "
                    "whole; reject it and propose the dispositions again"
                )

    pack.approvals = [approver] + ([second_approver] if second_approver else [])
    pack.status = pack.status.APPROVED
    pack.decided_at = _now()
    if chosen is not None:
        # The choice becomes the pack's body: exactly the chosen
        # candidate's allocations, at the figures the candidate was
        # scanned with (its recorded conversion included). The
        # unchosen candidates stay on the docket as lapsed.
        info = pack.resolution
        info.chosen_candidate = chosen.candidate_id
        for spec in chosen.allocations:
            alloc = Allocation(
                bank_line_id=info.bank_line_id,
                policy_number=spec.policy_number,
                installment_id=spec.installment_id,
                amount=spec.amount,
                tier=None,
                note=f"Resolution of {info.suspense_id}: candidate "
                     f"{chosen.candidate_id} ({chosen.interpretation}). "
                     f"{chosen.arithmetic}",
                currency=info.currency,
                fx_rate=spec.fx_rate,
                functional_amount=spec.functional_amount,
            )
            pack.entries.append(PostingEntry(
                kind="allocation", allocation=alloc,
                proposal_kind="resolution", proposed_by=chosen.proposed_by))
        store.save_allocations(
            [e.allocation for e in pack.entries if e.allocation], pack_id)
    store.save_pack(pack)

    # The only place receivables change.
    brokers = {b.broker_id: b for b in store.brokers()}
    # A resolution pack's allocations price like combination legs:
    # the candidate was proven at the rate in force on the day its
    # cash arrived — the bank line's value date — so the chosen
    # settlement applies at that rate, the price it was proposed
    # at, not the pack's (assembly) date rate.
    res_line_date = line_dates.get(pack.resolution.bank_line_id) \
        if pack.resolution is not None else None
    touched: dict[str, object] = {}
    for entry in pack.entries:
        if entry.kind == "allocation" and entry.allocation:
            alloc = entry.allocation
            rec = touched.get(alloc.policy_number) or store.receivable(alloc.policy_number)
            inst = next(i for i in rec.installments if i.installment_id == alloc.installment_id)
            # The settlement rate is the one in force on the pack's
            # value date — the day the cash was worked — not whatever
            # the broker's rate has become by approval day. The
            # exceptions are a combination leg and a resolution
            # allocation: both were proven with the cash priced at
            # the rate in force on the day it arrived, so they
            # apply at that rate, the price they were proposed at.
            if entry.combination_id:
                rate = comb_rates.get((entry.combination_id, alloc.bank_line_id))
            elif pack.resolution is not None and rec.broker_id:
                rate = brokers[rec.broker_id].rate_on(res_line_date or pack.value_date)
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

    # A resolution approval disposes of the suspense item itself:
    # the cash the gate just applied WAS the item's cash, so the
    # item closes and its UNIDENTIFIED exception closes with it —
    # and the decision lands on the item's own trail, naming the
    # chosen candidate and the approver, beside the scan that
    # produced it.
    if pack.resolution is not None and chosen is not None:
        info = pack.resolution
        item = next((i for i in store.suspense_items()
                     if i.suspense_id == info.suspense_id), None)
        if item is not None and item.status == "open":
            item.status = "resolved"
            item.resolved_on = pack.decided_at.date()
            store.save_suspense(item)
        for exc in store.exceptions(status="open"):
            if (exc.bank_line_id == info.bank_line_id
                    and exc.code.value == "UNIDENTIFIED"):
                exc.status = "resolved"
                store.update_exception(exc)
        lapsed = [c.candidate_id for c in info.candidates
                  if not c.flags and c.candidate_id != chosen.candidate_id]
        store.audit(AuditEvent(
            at=_now(), actor=approver, action="suspense_resolution_decided",
            subject=info.suspense_id,
            detail=f"resolution pack {pack_id} approved by {approver}: "
                   f"candidate {chosen.candidate_id} "
                   f"({chosen.policy_number}, {chosen.interpretation}) chosen"
                   + (f"; candidates {', '.join(lapsed)} lapsed to the "
                      "pack's record" if lapsed else "")))

    # A disposition approval moves the items themselves: out of
    # open suspense, into their disposition state — terminal, and
    # on the record with the kind, the assembler's reason, the
    # pack, and both approvers. The items are not deleted and
    # their amounts do not change; the close pack reads the state
    # and takes the cash out of the suspense identity through its
    # disposed term, from the pack's value date. The line's open
    # exceptions close with the item, as a combination settlement
    # closes its legs' exceptions: the cash they were about has
    # been disposed of, under dual control, in the open.
    if pack.disposition is not None:
        items_now = {i.suspense_id: i for i in store.suspense_items()}
        for d in pack.disposition.items:
            item = items_now[d.suspense_id]
            item.status = "dispositioned"
            item.disposition_kind = d.kind
            item.dispositioned_on = pack.value_date
            item.disposition_pack_id = pack_id
            item.disposition_reason = d.reason
            store.save_suspense(item)
            for exc in store.exceptions(status="open"):
                if exc.bank_line_id == item.bank_line_id:
                    exc.status = "resolved"
                    store.update_exception(exc)
            store.audit(AuditEvent(
                at=_now(), actor=approver,
                action="suspense_disposition_decided",
                subject=d.suspense_id,
                detail=f"disposition pack {pack_id} approved under dual "
                       f"control by {approver} and {second_approver} "
                       f"(assembled by {pack.assembled_by}): {d.amount} "
                       f"{d.currency} to {d.kind} — {d.reason}. The item "
                       "leaves open suspense as of the pack's value date "
                       f"{pack.value_date}; it stays on the record."))

    store.audit(AuditEvent(at=_now(), actor=approver, action="pack_approved", subject=pack_id,
                           detail=f"allocations {pack.allocation_total}, write-offs {pack.write_off_total}, "
                                  f"payable offsets {pack.offset_total}, "
                                  f"approvals: {', '.join(pack.approvals)}"))
    webhooks.emit(store, "pack_approved", pack_id, approver, {
        "allocations": as_str(pack.allocation_total),
        "write_offs": as_str(pack.write_off_total),
        "payable_offsets": as_str(pack.offset_total),
        "non_premium": as_str(pack.non_premium_total),
        "suspense_dispositions": as_str(pack.disposition_total),
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
        # The legs' cash stands in suspense again, so the pack must
        # stop standing in the allocations index: the index is what
        # the day report, the scorecard and the trend read as
        # "allocated", and a cent counted in both terms of the day
        # identity is a cent counted twice — the leg days stopped
        # footing the moment the items re-opened over rows that
        # still claimed them. No other rejection needs this: an
        # ordinary or re-match pack's rejection re-opens nothing,
        # so its rows remain the standing record of where its cash
        # sits. The combination pack's entries stay on the pack
        # record itself — the proposal, and its refusal, are
        # history; only the index lets go. (Any non-combination
        # allocations the pack carried — none, by construction —
        # would keep their rows.)
        store.replace_allocations(
            [e.allocation for e in pack.entries
             if e.allocation and not e.combination_id],
            pack.pack_id,
        )
    store.audit(AuditEvent(at=_now(), actor=actor, action="pack_rejected", subject=pack_id, detail=reason))
    if pack.resolution is not None:
        # The item was never touched — it stands open, as it did
        # while the pack stood proposed — and the candidates lapse
        # to the rejected pack's record. The decision lands on the
        # item's own trail so the next scan starts from the full
        # history of what was already considered.
        info = pack.resolution
        store.audit(AuditEvent(
            at=_now(), actor=actor, action="suspense_resolution_decided",
            subject=info.suspense_id,
            detail=f"resolution pack {pack_id} rejected by {actor}: {reason} "
                   f"Its {len(info.candidates)} candidate(s) lapse to the "
                   "pack's record; the cash stays in suspense."))
    if pack.disposition is not None:
        # A rejected disposition changes nothing: the items were
        # never held, so they stand open and aging, exactly as
        # they did while the pack stood proposed. The refusal
        # lands on each item's own trail, beside the proposal.
        for d in pack.disposition.items:
            store.audit(AuditEvent(
                at=_now(), actor=actor,
                action="suspense_disposition_decided",
                subject=d.suspense_id,
                detail=f"disposition pack {pack_id} rejected by {actor}: "
                       f"{reason} The proposed {d.kind} disposition of "
                       f"{d.amount} {d.currency} does not stand; the "
                       "item stays open in suspense."))
    webhooks.emit(store, "pack_rejected", pack_id, actor, {
        "allocations": as_str(pack.allocation_total),
        "write_offs": as_str(pack.write_off_total),
        "payable_offsets": as_str(pack.offset_total),
        "non_premium": as_str(pack.non_premium_total),
        "suspense_dispositions": as_str(pack.disposition_total),
    }, pack_kind="posting")
    return pack


def posting_file(pack: PostingPack) -> str:
    """The approved pack as a CSV posting file: what a human uploads
    to the accounting system. Rejected/proposed packs produce none.
    Every row names the currency its amount is denominated in, and a
    foreign-currency allocation also carries the conversion recorded
    when it was created — the rate and the functional amount — so
    the hand-off never mixes denominations silently."""
    if pack.status.value != "approved":
        raise GateError("a posting file exists only for an approved pack")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["pack_id", "value_date", "kind", "policy_number", "installment_id",
                     "bank_line_id", "amount", "tier", "approved_by",
                     "currency", "fx_rate", "functional_amount"])
    if pack.disposition is not None:
        # A disposition pack carries no entries: its rows are the
        # dispositions themselves — the write-offs to income a
        # human posts from this file, and the escheat reviews,
        # which post to the unclaimed-property holding, never to
        # income. The kind rides in the installment column, as a
        # non-premium entry's sub-kind does.
        for d in pack.disposition.items:
            writer.writerow([pack.pack_id, pack.value_date.isoformat(),
                             "suspense_disposition",
                             "", d.kind, d.bank_line_id,
                             f"{d.amount:.2f}", "",
                             ";".join(pack.approvals),
                             d.currency, "", f"{d.amount:.2f}"])
        return buf.getvalue()
    for entry in pack.entries:
        if entry.kind == "allocation" and entry.allocation:
            a = entry.allocation
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "premium_cash",
                             a.policy_number, a.installment_id, a.bank_line_id,
                             f"{a.amount:.2f}", a.tier.value if a.tier else "PROPOSED",
                             ";".join(pack.approvals),
                             a.currency,
                             str(a.fx_rate) if a.fx_rate is not None else "",
                             f"{(a.functional_amount if a.functional_amount is not None else a.amount):.2f}"])
        elif entry.kind == "non_premium" and entry.non_premium:
            np = entry.non_premium
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "non_premium",
                             "", np.kind, np.bank_line_id,
                             f"{np.amount:.2f}", "",
                             ";".join(pack.approvals),
                             np.currency, "", f"{np.amount:.2f}"])
        elif entry.kind == "payable_offset" and entry.payable_offset:
            o = entry.payable_offset
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "return_premium_offset",
                             o.policy_number, o.payable_id, o.bank_line_id,
                             f"{o.amount:.2f}", "", ";".join(pack.approvals),
                             o.currency, "", f"{o.amount:.2f}"])
        else:
            writer.writerow([pack.pack_id, pack.value_date.isoformat(), "write_off",
                             entry.write_off_policy or "", "", "",
                             f"{entry.write_off_amount:.2f}", "", ";".join(pack.approvals),
                             "", "", ""])
    return buf.getvalue()
