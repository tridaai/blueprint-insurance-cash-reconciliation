"""The suspense re-match pass.

Information lands after cash does: a broker's corrected advice
arrives weeks after the wire, or a policy is booked after its
payment. The re-match pass takes open suspense items — bank lines
with no allocations yet, the whole line unapplied — and works them
through the same tiered matcher against the books as they stand
now, using the same context construction the day run uses.

It is an explicit act (API, console button, CLI), never something
the day run does on its own. And it changes nothing about the
authority rules: a re-match proposes. Whatever it settles lands in
a `PACK-REMATCH-<date>` pack that stops at the same human gate; a
rejected or never-approved pack applies nothing, and the receivables
still change only in `gate.approve_pack`.

Every item in scope gets one audit event for the attempt — tried
at, tiers run, and the outcome: recovered (tier, pack), partially
settled (what remains and why), or still open (the reason, which may
be a different, now-correct exception code). The daily report shows
recoveries as their own line.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone

from . import combinations, gate, webhooks
from .config import Policy as PolicyConfig
from .matcher import MatchContext, match_line
from .models import AuditEvent, ExceptionCode
from .money import ZERO
from .pipeline import _expected_net, prepare_remittances
from .returns import fully_returned_line_ids
from .store import Store

log = logging.getLogger("cash_recon.rematch")

TIERS_RUN = "T1,T2,T3,T4,T5"  # the matcher always works the full ladder


class RematchContext(MatchContext):
    """A match context for cash whose explanation arrived late.

    The day-run pairing rule — the advice received the day the wire
    did — no longer applies, so the rule widens: from this broker,
    pair the first advice that is either of that day or whose total
    is the wire to the cent. Candidates are tried latest-first, so a
    corrected advice supersedes the one that failed. T4's own
    arithmetic validation still has the last word: a wrong pairing
    foots to nothing and allocates nothing."""

    def remittance_for(self, line, broker):
        for rem in self.remittances:
            if rem.remittance_id in self._consumed_remittances:
                continue
            if rem.broker_id != broker.broker_id:
                continue
            lines_total = sum((l.amount for l in rem.lines), ZERO)
            if (
                rem.received_date == line.value_date
                or rem.stated_total == line.amount
                or lines_total == line.amount
            ):
                self._consumed_remittances.add(rem.remittance_id)
                return rem
        return None


def _stamp(as_of: date) -> datetime:
    return datetime.combine(as_of, time(12, 0), tzinfo=timezone.utc)


def rematch_suspense(store: Store, reader, policy: PolicyConfig,
                     actor: str, as_of: date) -> dict:
    """Re-match open suspense against the books as of `as_of`.

    Scope: items still open, belonging to a bank line with no
    allocations yet, holding the line's full amount — the population
    the feature targets: money that landed with nothing to apply it
    to, and information that arrived after.
    """
    stamp = _stamp(as_of)
    prepared = prepare_remittances(store, reader, as_of)

    lines = {line.bank_line_id: line for line in store.bank_lines()}
    allocated = {a.bank_line_id for a in store.allocations()}
    approved = {
        entry.allocation.bank_line_id
        for pack in store.packs()
        if pack.status.value == "approved"
        for entry in pack.entries
        if entry.allocation
    }
    # Latest advice first: a corrected remittance supersedes the one
    # that failed, and nothing already allocated is touched twice.
    ctx = RematchContext(
        receivables={r.policy_number: r for r in store.receivables()},
        brokers={b.broker_id: b for b in store.brokers()},
        remittances=sorted(store.remittances(), key=lambda r: (r.received_date, r.remittance_id), reverse=True),
        prior_lines=sorted(store.bank_lines(), key=lambda l: (l.value_date, l.bank_line_id)),
        settled_line_ids=set(approved) | set(allocated),
        returned_line_ids=fully_returned_line_ids(store),
        payables={p.policy_number: p for p in store.payables(status="open")},
        proposer=reader,
        policy=policy,
    )

    scope = sorted(
        (
            i for i in store.suspense_items()
            if i.status == "open"
            and i.bank_line_id in lines
            and i.bank_line_id not in allocated
            and i.amount == lines[i.bank_line_id].amount
        ),
        key=lambda i: (i.received_date, i.suspense_id),
    )

    recovered, partial, still_open, outcomes, attempts = [], [], [], [], []
    for item in scope:
        line = lines[item.bank_line_id]
        start_amount = item.amount
        outcome = match_line(line, ctx)
        if outcome.attempts:
            # The re-match's ladder joins the line's own trail, so
            # the audit export shows every working of the line, the
            # day run's and each re-match's alike.
            store.audit(AuditEvent(at=stamp, actor=actor, action="line_attempts",
                                   subject=line.bank_line_id,
                                   detail=json.dumps(outcome.attempts)))
        if outcome.allocations:
            ctx.settled_line_ids.add(line.bank_line_id)

        for exc in outcome.exceptions:
            if exc.code == ExceptionCode.NETTING_MISMATCH:
                broker = ctx.brokers.get(exc.broker_id) if exc.broker_id else None
                note = reader.draft_broker_note(
                    broker.name if broker else "the broker",
                    exc.policy_number or "",
                    _expected_net(ctx, exc),
                    line.amount,
                )
                store.audit(AuditEvent(at=stamp, actor=f"reader:{reader.name}",
                                       action="broker_note_drafted", subject=line.bank_line_id,
                                       detail=note))

        open_exceptions = [
            e for e in store.exceptions(status="open") if e.bank_line_id == line.bank_line_id
        ]
        if outcome.tier:
            tier_name = outcome.tier.value
        elif outcome.offsets:
            tier_name = "netted against a return-premium payable"
        else:
            tier_name = f"proposal by {outcome.proposed_by}"
        settled_amount = outcome.allocated_total + outcome.offset_total
        if settled_amount > ZERO and outcome.suspense_amount == ZERO:
            for e in open_exceptions:
                e.status = "resolved"
                store.update_exception(e)
            for exc in outcome.exceptions:
                store.add_exception(exc)
            item.status = "resolved"
            item.resolved_on = as_of
            store.save_suspense(item)
            outcomes.append(outcome)
            recovered.append({
                "suspense_id": item.suspense_id, "bank_line_id": line.bank_line_id,
                "amount": str(start_amount), "tier": tier_name,
            })
            attempts.append((
                item, f"tiers run {TIERS_RUN}; matched at {tier_name} — {start_amount} leaves "
                      f"suspense into the re-match pack"
            ))
            log.info("rematch %s: rescued at %s", line.bank_line_id, tier_name)
        elif settled_amount > ZERO:
            for e in open_exceptions:
                e.status = "resolved"
                store.update_exception(e)
            for exc in outcome.exceptions:
                store.add_exception(exc)
            item.amount = outcome.suspense_amount
            item.reason = outcome.exceptions[0].code.value if outcome.exceptions else "Unallocated remainder"
            store.save_suspense(item)
            outcomes.append(outcome)
            partial.append({
                "suspense_id": item.suspense_id, "bank_line_id": line.bank_line_id,
                "settled": str(settled_amount),
                "remaining": str(outcome.suspense_amount),
                "tier": tier_name,
            })
            attempts.append((
                item, f"tiers run {TIERS_RUN}; partially settled at {tier_name} — "
                      f"{settled_amount} settled, {outcome.suspense_amount} "
                      f"still open as {item.reason}"
            ))
            log.info("rematch %s: partial at %s", line.bank_line_id, tier_name)
        else:
            for exc in outcome.exceptions:
                store.add_exception(exc)
            reason = outcome.exceptions[0].code.value if outcome.exceptions else "NO_TIER_SETTLED"
            if outcome.exceptions:
                item.reason = reason
                store.save_suspense(item)
            still_open.append({
                "suspense_id": item.suspense_id, "bank_line_id": line.bank_line_id,
                "amount": str(item.amount), "reason": reason,
            })
            attempts.append((
                item, f"tiers run {TIERS_RUN}; still open: {reason}"
                       + (f" — {outcome.exceptions[0].detail}" if outcome.exceptions else "")
            ))
            log.info("rematch %s: still open (%s)", line.bank_line_id, reason)

    pack_id = None
    if outcomes:
        pack = gate.assemble_pack(as_of, outcomes, actor, policy)
        pack.pack_id = f"PACK-REMATCH-{as_of.isoformat()}"
        existing = store.pack(pack.pack_id)
        if existing is not None and existing.status.value == "proposed":
            have = {
                (e.allocation.bank_line_id, e.allocation.installment_id)
                for e in existing.entries if e.allocation
            }
            for entry in pack.entries:
                key = (entry.allocation.bank_line_id, entry.allocation.installment_id)
                if key not in have:
                    existing.entries.append(entry)
            pack = existing
        elif existing is not None:
            n = 2
            while store.pack(f"{pack.pack_id}-{n}") is not None:
                n += 1
            pack.pack_id = f"{pack.pack_id}-{n}"
        store.save_pack(pack)
        store.save_allocations(
            [a for outcome in outcomes for a in outcome.allocations], pack.pack_id
        )
        store.audit(AuditEvent(at=stamp, actor=actor, action="pack_assembled", subject=pack.pack_id,
                               detail=f"{len(pack.entries)} re-match entries, allocations "
                                      f"{pack.allocation_total}; stops at the gate"))
        webhooks.emit(store, "pack_assembled", pack.pack_id, actor, {
            "allocations": str(pack.allocation_total),
            "entries": str(len(pack.entries)),
        }, pack_kind="posting")
        pack_id = pack.pack_id

    for item, detail in attempts:
        store.audit(AuditEvent(at=stamp, actor=actor, action="suspense_rematch_attempt",
                               subject=item.suspense_id, detail=detail))

    # The combination pass closes the re-match: two items the tiers
    # could not settle alone may settle one bill together. It reads
    # the books as the re-match left them — items recovered above
    # are already resolved and out of the leg population — and its
    # proposals land in a PACK-COMB pack at the same gate.
    comb = combinations.propose_combinations(store, actor, as_of)

    return {
        "as_of": as_of.isoformat(),
        "actor": actor,
        "documents_prepared": prepared,
        "attempted": len(scope),
        "recovered": recovered,
        "partially_recovered": partial,
        "still_open": still_open,
        "pack_id": pack_id,
        "combinations": comb["proposed"],
        "combination_pack_id": comb["pack_id"],
    }
