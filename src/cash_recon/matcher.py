"""The tiered matcher. Deterministic, in code, in order.

Every bank line is worked through the tiers exactly once:

  T1 exact        amount + policy reference, amount equals the amount due
  T2 netting      amount equals billed minus the broker's contracted
                  commission, to the cent
  T3 reference    policy reference present, amount differs — allocate
                  what is there, never force a match; the delta becomes
                  a PARTIAL / OVERPAID exception
  T4 remittance   the line is explained by a remittance advice; its
                  lines are allocated per policy and validated
  T5 unidentified no reference, no remittance, no defensible proposal

Reference-led payments are read against the next open installment
unless they exactly match a wider scope (the full open balance, or an
exact run of consecutive installments). A broker payment that settles
nothing exactly and lands below the amount due is a netting mismatch
when the implied commission rate is a clean rate that is not the
contracted one — that is what a broker deducting the wrong rate looks
like — and a partial payment otherwise.

Installments are individual open items, and the allocation order is
code, stated on the record: cash identified for a policy applies to
the oldest open installment first. The one exception is a payer whose
structured reference names a specific installment (NSP-1003-I3) —
that installment is settled first and any remainder flows to the
oldest open installment after it, with the flow written on the
allocations. A free-text note indicating a different installment is
not an instruction: the cash still applies oldest-first, and the
outcome carries an INTENT_DIFFERS flag so the desk sees the payer's
apparent intent differed. Money is never left in suspense over a
guess about intent.

Every line ends fully accounted for: allocations + suspense == the
bank amount, to the cent. The matcher asserts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from .config import Policy as PolicyConfig
from .llm import Candidate
from .models import (
    Allocation,
    BankLine,
    Broker,
    ExceptionCode,
    ExceptionRecord,
    PolicyStatus,
    Receivable,
    Remittance,
    Tier,
)
from .money import ZERO, cents, net_of

POLICY_RE = re.compile(r"NSP-\d{4}")

# A free-text note indicating an installment ("installment 2", "inst. 3").
INTENT_RE = re.compile(r"(?:instal{1,2}ment|inst)\.?\s*(?:no\.?|#)?\s*(\d+)", re.IGNORECASE)


class LineOutcome(BaseModel):
    bank_line_id: str
    allocations: list[Allocation] = []
    exceptions: list[ExceptionRecord] = []
    suspense_amount: Decimal = ZERO
    tier: Tier | None = None
    proposed_by: str | None = None
    notes: list[str] = []

    @property
    def allocated_total(self) -> Decimal:
        return sum((a.amount for a in self.allocations), ZERO)


@dataclass
class MatchContext:
    receivables: dict[str, Receivable]
    brokers: dict[str, Broker]
    remittances: list[Remittance]  # read, screened, not yet consumed
    prior_lines: list[BankLine]
    settled_line_ids: set[str]  # earlier lines that produced allocations
    proposer: object  # reader backend (propose_lump_sum)
    policy: PolicyConfig
    _consumed_remittances: set[str] = field(default_factory=set)

    def broker_for_payer(self, payer: str) -> Broker | None:
        norm = _norm(payer)
        for broker in self.brokers.values():
            if _norm(broker.name) in norm or norm in _norm(broker.name):
                return broker
        return None

    def remittance_for(self, line: BankLine, broker: Broker) -> Remittance | None:
        for rem in self.remittances:
            if rem.remittance_id in self._consumed_remittances:
                continue
            if rem.broker_id == broker.broker_id and rem.received_date == line.value_date:
                self._consumed_remittances.add(rem.remittance_id)
                return rem
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def cash_due(rec: Receivable, installment, brokers: dict[str, Broker]) -> Decimal:
    """Cash that settles an installment: net of commission for broker
    business, the billed amount for direct business."""
    if rec.broker_id:
        return net_of(installment.open_balance, brokers[rec.broker_id].commission_rate)
    return installment.open_balance


def _exc(line, code, amount, detail, policy_number=None, broker_id=None, proposed_by=None):
    return ExceptionRecord(
        exception_id=f"EXC-{line.bank_line_id}-{code.value}"
        + (f"-{policy_number}" if policy_number and code in (ExceptionCode.UNKNOWN_POLICY, ExceptionCode.CANCELLED_POLICY) else ""),
        code=code,
        bank_line_id=line.bank_line_id,
        policy_number=policy_number,
        broker_id=broker_id,
        amount=cents(amount),
        detail=detail,
        first_seen=line.value_date,
        proposed_by=proposed_by,
    )


def _distribute(line, rec, amount, dues, tier, note=""):
    """Allocate cash across open installments, oldest first, on the
    given per-installment due basis. Returns (allocations, leftover)."""
    out: list[Allocation] = []
    remaining = amount
    for inst, due in zip(rec.open_installments(), dues):
        if remaining <= 0:
            break
        take = min(remaining, due)
        if take > 0:
            out.append(
                Allocation(bank_line_id=line.bank_line_id, policy_number=rec.policy_number,
                           installment_id=inst.installment_id, amount=take, tier=tier, note=note)
            )
            remaining -= take
    return out, remaining


def match_line(line: BankLine, ctx: MatchContext) -> LineOutcome:
    outcome = LineOutcome(bank_line_id=line.bank_line_id)
    broker = ctx.broker_for_payer(line.payer_name)

    # -- remittance-led (T4): the day's advice from this broker ----------
    if broker is not None and line.reference is None:
        rem = ctx.remittance_for(line, broker)
        if rem is not None:
            return _match_remittance(line, rem, ctx, outcome)

    # -- duplicate check, in code, before any tier -------------------------
    dup = _find_duplicate(line, ctx)
    if dup is not None:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.DUPLICATE_PAYMENT, line.amount,
                 f"Same payer and amount as {dup.bank_line_id} "
                 f"({dup.value_date}), which already produced allocations. "
                 "Held in suspense for a human to confirm with the payer.",
                 broker_id=broker.broker_id if broker else None)
        )
        outcome.suspense_amount = line.amount
        return _finish(line, outcome)

    # -- reference-led -------------------------------------------------------
    ref = _reference_of(line)
    rec = ctx.receivables.get(ref) if ref else None
    if rec is None and ref is None:
        # no reference: payer attribution or the lump-sum proposer
        rec = _policy_for_payer(line, ctx)
        if rec is not None:
            outcome.notes.append("Attributed to a policy by payer name.")
    if ref and rec is None:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.UNKNOWN_POLICY, line.amount,
                 f"Reference {ref} is not a policy on the books. Held in suspense.",
                 policy_number=ref)
        )
        outcome.suspense_amount = line.amount
        return _finish(line, outcome)
    if rec is not None:
        outcome = _match_reference(line, rec, ctx, outcome, broker)
        _flag_intent(line, rec, outcome)
        return outcome

    # -- lump-sum proposal (broker payer, no reference, no advice) ---------
    if broker is not None:
        proposal = _propose_lump_sum(line, broker, ctx)
        if proposal is not None:
            return proposal

    # -- T5: unidentified ------------------------------------------------------
    outcome.exceptions.append(
        _exc(line, ExceptionCode.UNIDENTIFIED, line.amount,
             "No policy reference, no remittance advice, and no defensible "
             "allocation proposal. To suspense for identification.")
    )
    outcome.suspense_amount = line.amount
    return _finish(line, outcome)


def _reference_of(line: BankLine) -> str | None:
    for text in (line.reference or "", line.narrative or ""):
        m = POLICY_RE.search(text)
        if m:
            return m.group(0)
    return None


def _named_installment(line: BankLine, rec: Receivable):
    """The installment the payer's structured reference names, if any
    (NSP-1003-I3, NSP-1003/3). The structured reference is the one
    place a payer can instruct the allocation order; free text is
    only ever an indication (see INTENT_DIFFERS)."""
    ref = line.reference or ""
    m = re.search(rf"{re.escape(rec.policy_number)}[-/](?:I)?(\d+)\b", ref)
    if not m:
        return None
    seq = int(m.group(1))
    return next((i for i in rec.installments if i.sequence == seq), None)


def _indicated_installment(line: BankLine, rec: Receivable) -> int | None:
    """The installment the payer appears to intend, when that intent
    was not an instruction the named path could follow: a reference
    naming an already-settled installment, or a free-text note."""
    named = _named_installment(line, rec)
    if named is not None:
        return named.sequence if named.open_balance <= 0 else None
    m = INTENT_RE.search(line.narrative or "")
    if m:
        seq = int(m.group(1))
        if any(i.sequence == seq for i in rec.installments):
            return seq
    return None


def _seq_of(installment_id: str) -> int | None:
    try:
        return int(installment_id.rsplit("-I", 1)[1])
    except (IndexError, ValueError):
        return None


def _flag_intent(line: BankLine, rec: Receivable, outcome: LineOutcome) -> None:
    """If the payer's apparent intent pointed at an installment other
    than the oldest open one, the cash has still been applied
    oldest-first — this flag is how the desk sees the difference."""
    indicated = _indicated_installment(line, rec)
    if indicated is None:
        return
    open_insts = rec.open_installments()
    if not open_insts or open_insts[0].sequence == indicated:
        return
    if any(e.code == ExceptionCode.INTENT_DIFFERS for e in outcome.exceptions):
        return
    elsewhere = sum(
        (a.amount for a in outcome.allocations if _seq_of(a.installment_id) != indicated),
        ZERO,
    )
    outcome.exceptions.append(
        _exc(line, ExceptionCode.INTENT_DIFFERS, elsewhere,
             f"The payer's note indicates installment {indicated} of "
             f"{rec.policy_number}, but no installment was named in the "
             "reference, so the cash was applied oldest-open-installment "
             "first, per the allocation rule. The money is applied; the "
             "difference of intent is on the record for the desk.",
             policy_number=rec.policy_number, broker_id=rec.broker_id)
    )
    outcome.notes.append(
        f"Payer intent indicated installment {indicated}; applied oldest-first (INTENT_DIFFERS)."
    )


def _policy_for_payer(line: BankLine, ctx: MatchContext) -> Receivable | None:
    norm = _norm(line.payer_name)
    hits = [r for r in ctx.receivables.values() if _norm(r.insured_name) == norm]
    return hits[0] if len(hits) == 1 else None


def _find_duplicate(line: BankLine, ctx: MatchContext) -> BankLine | None:
    for prior in ctx.prior_lines:
        if prior.bank_line_id == line.bank_line_id:
            continue
        if prior.bank_line_id not in ctx.settled_line_ids:
            continue
        if _norm(prior.payer_name) != _norm(line.payer_name):
            continue
        if prior.amount != line.amount:
            continue
        if abs((line.value_date - prior.value_date).days) <= ctx.policy.duplicate_window_days:
            return prior
    return None


def _match_reference(line, rec: Receivable, ctx, outcome, payer_broker) -> LineOutcome:
    policy_broker = ctx.brokers.get(rec.broker_id) if rec.broker_id else None
    if rec.status == PolicyStatus.CANCELLED:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.CANCELLED_POLICY, line.amount,
                 f"Policy {rec.policy_number} was cancelled before this cash "
                 "arrived. Held in suspense; refund or reallocation is a human call.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        outcome.suspense_amount = line.amount
        return _finish(line, outcome)

    insts = rec.open_installments()
    if not insts:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.OVERPAID, line.amount,
                 f"Policy {rec.policy_number} has no open balance. "
                 "Held in suspense as an overpayment.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        outcome.suspense_amount = line.amount
        return _finish(line, outcome)

    gross = [i.open_balance for i in insts]
    cash = [cash_due(rec, i, ctx.brokers) for i in insts]
    amount = line.amount
    next_gross, next_cash = gross[0], cash[0]
    full_gross, full_cash = sum(gross, ZERO), sum(cash, ZERO)

    # A named installment is settled first; the rest of the ladder is
    # for payments whose reference names no installment.
    named = _named_installment(line, rec)
    if named is not None and named.open_balance > 0:
        return _match_named(line, rec, named, ctx, outcome)

    # T1: exact against the next installment or the full balance, gross basis
    if amount == next_gross or amount == full_gross:
        dues = gross if amount == next_gross else gross
        allocs, _ = _distribute(line, rec, amount, dues, Tier.T1_EXACT)
        outcome.allocations, outcome.tier = allocs, Tier.T1_EXACT
        return _finish(line, outcome)

    # T2: netting-exact on the same scopes
    if policy_broker is not None and (amount == next_cash or amount == full_cash):
        allocs, _ = _distribute(line, rec, amount, cash, Tier.T2_NETTING)
        outcome.allocations, outcome.tier = allocs, Tier.T2_NETTING
        return _finish(line, outcome)

    # T3: an exact run of consecutive installments (combined payment)
    for basis, label in ((cash, "net"), (gross, "gross")):
        run = ZERO
        for k in range(2, len(insts) + 1):
            run = sum(basis[:k], ZERO)
            if run == amount:
                allocs, _ = _distribute(line, rec, amount, basis, Tier.T3_REFERENCE,
                                        note=f"Combined payment: {k} installments, {label} basis.")
                outcome.allocations, outcome.tier = allocs, Tier.T3_REFERENCE
                return _finish(line, outcome)

    # Overpaid against the whole policy
    if amount > full_gross:
        allocs, _ = _distribute(line, rec, full_gross, gross, Tier.T3_REFERENCE)
        excess = amount - full_gross
        outcome.allocations, outcome.tier = allocs, Tier.T3_REFERENCE
        outcome.exceptions.append(
            _exc(line, ExceptionCode.OVERPAID, excess,
                 f"Payment exceeds the open balance of {rec.policy_number} by {excess}. "
                 "Excess held in suspense.", policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        outcome.suspense_amount = excess
        return _finish(line, outcome)

    # Overpaid against the next installment only (single-scope payment)
    if amount > next_cash and amount < (cash[0] + cash[1] if len(cash) > 1 else full_cash + 1):
        allocs, _ = _distribute(line, rec, next_cash, cash, Tier.T3_REFERENCE)
        excess = amount - next_cash
        outcome.allocations, outcome.tier = allocs, Tier.T3_REFERENCE
        outcome.exceptions.append(
            _exc(line, ExceptionCode.OVERPAID, excess,
                 f"Payment exceeds the amount due on the next installment of "
                 f"{rec.policy_number} by {excess}. Excess held in suspense.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        outcome.suspense_amount = excess
        return _finish(line, outcome)

    # Under the next installment's cash due: netting mismatch or partial
    if amount < next_cash:
        if policy_broker is not None and len(insts) == 1:
            implied = (next_gross - amount) / next_gross
            contracted = policy_broker.commission_rate
            clean_rate = (implied * 1000) == (implied * 1000).to_integral_value()
            if clean_rate and implied != contracted:
                outcome.exceptions.append(
                    _exc(line, ExceptionCode.NETTING_MISMATCH, next_cash - amount,
                         f"{policy_broker.name} remitted {amount} against {rec.policy_number}: "
                         f"the contracted {contracted:.1%} rate expects {next_cash}, but the "
                         f"payment implies a {implied:.1%} deduction. Cash held unapplied; "
                         "a query note has been drafted for review.",
                         policy_number=rec.policy_number, broker_id=rec.broker_id)
                )
                outcome.suspense_amount = amount
                return _finish(line, outcome)
        allocs, _ = _distribute(line, rec, amount, cash, Tier.T3_REFERENCE)
        outcome.allocations, outcome.tier = allocs, Tier.T3_REFERENCE
        outcome.exceptions.append(
            _exc(line, ExceptionCode.PARTIAL, next_cash - amount,
                 f"Payment is {next_cash - amount} short of the amount due on "
                 f"{rec.policy_number}. The balance stays open and collectable.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        return _finish(line, outcome)

    # Between the next installment and a wider scope, not exact: apply in
    # order on the cash basis; the tail is a visible part-payment.
    allocs, _ = _distribute(line, rec, amount, cash, Tier.T3_REFERENCE,
                            note="Applied across open installments, oldest open first.")
    outcome.allocations, outcome.tier = allocs, Tier.T3_REFERENCE
    return _finish(line, outcome)


def _distribute_pairs(line, pairs, amount, tier, note=""):
    """Allocate across an explicit ordered list of (installment, due)
    pairs. Returns (allocations, leftover)."""
    out: list[Allocation] = []
    remaining = amount
    for inst, due in pairs:
        if remaining <= 0:
            break
        take = min(remaining, due)
        if take > 0:
            out.append(
                Allocation(bank_line_id=line.bank_line_id, policy_number=inst.policy_number,
                           installment_id=inst.installment_id, amount=take, tier=tier, note=note)
            )
            remaining -= take
    return out, remaining


def _match_named(line, rec: Receivable, named, ctx, outcome) -> LineOutcome:
    """The payer's reference names this installment: settle it first.

    Exact settlement keeps its tier (gross = T1, net of commission =
    T2). A short payment is a PARTIAL against the named installment —
    it does not spill onto earlier ones the payer did not mean. An
    overpayment settles the named installment and the remainder flows
    to the oldest open installment after it (then to any other open
    installments, oldest first); beyond the policy's full remaining
    balance it is OVERPAID, as everywhere else. The flow is written
    on the allocations and in the outcome notes."""
    gross_due = named.open_balance
    cash_due_named = cash_due(rec, named, ctx.brokers)
    amount = line.amount
    note = f"Named installment {named.installment_id}, per the payer's reference."

    if amount == gross_due:
        outcome.allocations.append(
            Allocation(bank_line_id=line.bank_line_id, policy_number=rec.policy_number,
                       installment_id=named.installment_id, amount=gross_due,
                       tier=Tier.T1_EXACT, note=note))
        outcome.tier = Tier.T1_EXACT
        return _finish(line, outcome)
    if rec.broker_id and amount == cash_due_named:
        outcome.allocations.append(
            Allocation(bank_line_id=line.bank_line_id, policy_number=rec.policy_number,
                       installment_id=named.installment_id, amount=cash_due_named,
                       tier=Tier.T2_NETTING, note=note))
        outcome.tier = Tier.T2_NETTING
        return _finish(line, outcome)
    if amount < cash_due_named:
        outcome.allocations.append(
            Allocation(bank_line_id=line.bank_line_id, policy_number=rec.policy_number,
                       installment_id=named.installment_id, amount=amount,
                       tier=Tier.T3_REFERENCE, note=note))
        outcome.tier = Tier.T3_REFERENCE
        outcome.exceptions.append(
            _exc(line, ExceptionCode.PARTIAL, cash_due_named - amount,
                 f"Payment naming installment {named.sequence} of {rec.policy_number} "
                 f"is {cash_due_named - amount} short of that installment's amount due. "
                 "The balance stays open and collectable.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        return _finish(line, outcome)

    # Over the named installment: settle it on the basis the payment
    # reaches, then flow the remainder forward in sequence order.
    if amount >= gross_due:
        settled, basis = gross_due, "gross"
    else:
        settled, basis = cash_due_named, "cash"
    outcome.allocations.append(
        Allocation(bank_line_id=line.bank_line_id, policy_number=rec.policy_number,
                   installment_id=named.installment_id, amount=settled,
                   tier=Tier.T3_REFERENCE, note=note))
    remainder = amount - settled
    others = [i for i in rec.open_installments() if i.sequence != named.sequence]
    ordered = [i for i in others if i.sequence > named.sequence] + \
              [i for i in others if i.sequence < named.sequence]
    dues = [
        i.open_balance if basis == "gross" else cash_due(rec, i, ctx.brokers)
        for i in ordered
    ]
    flow_note = (f"Remainder of the payment naming {named.installment_id}, applied to "
                 "the oldest open installment after it.")
    flow, leftover = _distribute_pairs(line, list(zip(ordered, dues)), remainder,
                                       Tier.T3_REFERENCE, note=flow_note)
    outcome.allocations.extend(flow)
    outcome.tier = Tier.T3_REFERENCE
    flowed_to = ", ".join(a.installment_id for a in flow) or "none"
    outcome.notes.append(
        f"Named installment {named.installment_id} settled first ({settled}, {basis} basis); "
        f"remainder {remainder} flowed to: {flowed_to}."
    )
    if leftover > 0:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.OVERPAID, leftover,
                 f"Payment naming installment {named.sequence} of {rec.policy_number} "
                 f"exceeds the policy's open balance by {leftover}. Excess held in suspense.",
                 policy_number=rec.policy_number, broker_id=rec.broker_id)
        )
        outcome.suspense_amount = leftover
    return _finish(line, outcome)


def _match_remittance(line, rem: Remittance, ctx, outcome) -> LineOutcome:
    lines_total = sum((l.amount for l in rem.lines), ZERO)
    problems: list[str] = []
    if rem.stated_total is not None and lines_total != rem.stated_total:
        problems.append(f"its lines sum to {lines_total} but the advice states {rem.stated_total}")
    if lines_total != line.amount:
        problems.append(f"its lines sum to {lines_total} but the wire was {line.amount}")
    if problems:
        outcome.exceptions.append(
            _exc(line, ExceptionCode.REMITTANCE_ARITHMETIC, abs(lines_total - line.amount),
                 f"Remittance {rem.remittance_id} does not foot: " + "; ".join(problems) +
                 ". Nothing allocated; the wire is held in suspense for the broker to correct.",
                 broker_id=rem.broker_id)
        )
        outcome.suspense_amount = line.amount
        return _finish(line, outcome)

    for rline in rem.lines:
        rec = ctx.receivables.get(rline.policy_number)
        if rec is None:
            outcome.exceptions.append(
                _exc(line, ExceptionCode.UNKNOWN_POLICY, rline.amount,
                     f"Remittance {rem.remittance_id} cites {rline.policy_number}, which is "
                     "not a policy on the books. That portion is held in suspense.",
                     policy_number=rline.policy_number, broker_id=rem.broker_id)
            )
            outcome.suspense_amount += rline.amount
            continue
        if rec.status == PolicyStatus.CANCELLED:
            outcome.exceptions.append(
                _exc(line, ExceptionCode.CANCELLED_POLICY, rline.amount,
                     f"Remittance {rem.remittance_id} pays {rline.policy_number}, which was "
                     "cancelled. That portion is held in suspense.",
                     policy_number=rline.policy_number, broker_id=rem.broker_id)
            )
            outcome.suspense_amount += rline.amount
            continue
        insts = rec.open_installments()
        dues = [cash_due(rec, i, ctx.brokers) for i in insts]
        total_due = sum(dues, ZERO)
        if total_due == 0:
            outcome.exceptions.append(
                _exc(line, ExceptionCode.OVERPAID, rline.amount,
                     f"Remittance {rem.remittance_id} pays {rline.policy_number}, which has "
                     "no open balance. That portion is held in suspense.",
                     policy_number=rline.policy_number, broker_id=rem.broker_id)
            )
            outcome.suspense_amount += rline.amount
            continue
        allocs, _ = _distribute(line, rec, min(rline.amount, total_due), dues,
                                Tier.T4_REMITTANCE, note=f"Per remittance {rem.remittance_id}.")
        outcome.allocations.extend(allocs)
        if rline.amount > total_due:
            excess = rline.amount - total_due
            outcome.exceptions.append(
                _exc(line, ExceptionCode.OVERPAID, excess,
                     f"Remittance line for {rline.policy_number} exceeds its open balance "
                     f"by {excess}. Excess held in suspense.",
                     policy_number=rline.policy_number, broker_id=rem.broker_id)
            )
            outcome.suspense_amount += excess
        elif dues and rline.amount < dues[0]:
            outcome.exceptions.append(
                _exc(line, ExceptionCode.PARTIAL, dues[0] - rline.amount,
                     f"Remittance line for {rline.policy_number} is short of the "
                     "next installment due. The balance stays open.",
                     policy_number=rline.policy_number, broker_id=rem.broker_id)
            )
    if outcome.allocations:
        outcome.tier = Tier.T4_REMITTANCE
    return _finish(line, outcome)


def _propose_lump_sum(line, broker, ctx) -> LineOutcome | None:
    candidates: list[Candidate] = []
    for rec in sorted(ctx.receivables.values(), key=lambda r: r.policy_number):
        if rec.broker_id != broker.broker_id or rec.status == PolicyStatus.CANCELLED:
            continue
        for inst in rec.open_installments():
            candidates.append(Candidate(policy_number=rec.policy_number,
                                        installment_id=inst.installment_id,
                                        cash_due=cash_due(rec, inst, ctx.brokers)))
    if not candidates:
        return None
    proposal = ctx.proposer.propose_lump_sum(line.amount, line.payer_name, candidates)
    if proposal is None:
        return None
    outcome = LineOutcome(bank_line_id=line.bank_line_id, proposed_by=proposal.backend)
    for cand in proposal.allocations:
        outcome.allocations.append(
            Allocation(bank_line_id=line.bank_line_id, policy_number=cand.policy_number,
                       installment_id=cand.installment_id, amount=cand.cash_due, tier=None,
                       note=f"Lump-sum proposal ({proposal.backend}): {proposal.reasoning}")
        )
    outcome.notes.append(f"Proposal by {proposal.backend}; validated in code before the gate.")
    return _finish(line, outcome)


def _finish(line: BankLine, outcome: LineOutcome) -> LineOutcome:
    accounted = outcome.allocated_total + outcome.suspense_amount
    assert accounted == line.amount, (
        f"{line.bank_line_id}: allocations {outcome.allocated_total} + suspense "
        f"{outcome.suspense_amount} != bank amount {line.amount}"
    )
    return outcome
