"""Return premiums: the other direction of cash.

Everything else in this blueprint is money arriving. A policy
cancelled mid-term reverses the flow: the carrier owes the unearned
premium back. This module records the cancellation and computes
what is owed, in code, with the computation on the record:

- **Earned premium is pro-rata by days**: billed premium × term
  days elapsed at the cancellation's effective date ÷ term days,
  half-up at the cent — the one defined computation, stated here
  and printed on the cancellation record line by line.
- **Paid to date is what the installments say**: the sum of
  `amount_applied` over the policy's installments, on the billed
  basis (broker cash settles gross, so this is the premium the
  carrier has actually taken).
- **The return premium is paid − earned**, when positive. When the
  insured is behind — earned ahead of paid — the record shows what
  they owe and no payable opens: the desk bills it, this system
  does not invent a payout for it.

The payable is owed to the insured, or to the broker of record
where the broker collected the premium — stated on the payable,
because the money goes back the way it came in. Recording a
cancellation is a named act with named refusals, like recording a
return: the policy must be on the books, not already cancelled,
and its record must carry term dates — a pro-rata computation
with nothing to stand on is refused, in words, not guessed.

What happens to the payable afterwards — netting against later
cash, payout packs at the gate — lives with the matcher and in
`payouts.py`. The policy's unpaid installments are left exactly
as they stand: cancellation stops the policy, it does not rewrite
its billing history, and the matcher already refuses fresh cash
against a cancelled policy except through the payable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from .models import (
    AuditEvent,
    Cancellation,
    PayableEvent,
    PolicyStatus,
    ReturnPremiumPayable,
)
from .money import ZERO, cents
from .store import Store


class CancellationError(RuntimeError):
    """A cancellation recording was refused. The message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def pro_rata_earned(premium_total: Decimal, term_start: date, term_end: date,
                    effective: date) -> tuple[Decimal, int, int]:
    """(earned premium, term days, elapsed days) at `effective`.

    The term is [term_start, term_end); elapsed days clamp to the
    term. Earned is premium × elapsed ÷ term days, rounded half-up
    at the cent — the same single rounding discipline as the
    netting math."""
    term_days = (term_end - term_start).days
    elapsed = min(max((effective - term_start).days, 0), term_days)
    return cents(premium_total * elapsed / term_days), term_days, elapsed


def record_cancellation(store: Store, policy_number: str, effective_date: date,
                        recorded_by: str, recorded_on: date,
                        reason: str = "") -> Cancellation:
    """Record a mid-term cancellation and open its return-premium
    payable when one is due. Raises CancellationError, with the
    reason stated, when the cancellation cannot be recorded."""
    if not recorded_by or not recorded_by.strip():
        raise CancellationError("a cancellation is recorded by a named person")
    rec = store.receivable(policy_number)
    if rec is None:
        raise CancellationError(f"no such policy on the books: {policy_number}")
    if rec.status == PolicyStatus.CANCELLED or store.cancellation_for(policy_number):
        raise CancellationError(
            f"policy {policy_number} is already cancelled; a cancellation is recorded once"
        )
    if rec.term_start is None or rec.term_end is None:
        raise CancellationError(
            f"policy {policy_number} carries no term dates on its record, and the "
            "pro-rata return-premium computation has nothing to stand on; book the "
            "term dates first"
        )
    if rec.term_end <= rec.term_start:
        raise CancellationError(
            f"policy {policy_number}'s term dates are degenerate "
            f"({rec.term_start} to {rec.term_end}); fix the record first"
        )
    if not (rec.term_start <= effective_date < rec.term_end):
        raise CancellationError(
            f"cancellation effective {effective_date} is outside the policy term "
            f"{rec.term_start} to {rec.term_end}: before the term there is nothing "
            "to cancel, and at or after its end the premium is fully earned"
        )

    premium_total = sum((i.amount_due for i in rec.installments), ZERO)
    paid_to_date = sum((i.amount_applied for i in rec.installments), ZERO)
    earned, term_days, elapsed = pro_rata_earned(
        premium_total, rec.term_start, rec.term_end, effective_date
    )
    unearned_days = term_days - elapsed
    difference = paid_to_date - earned
    return_premium = difference if difference > ZERO else ZERO
    insured_owes = -difference if difference < ZERO else ZERO

    if rec.broker_id:
        broker = next((b for b in store.brokers() if b.broker_id == rec.broker_id), None)
        payee_kind = "broker"
        payee_name = broker.name if broker else rec.broker_id
    else:
        payee_kind = "insured"
        payee_name = rec.insured_name

    payable_id = f"PAY-{policy_number}" if return_premium > ZERO else None
    computation = [
        f"Billed premium (all {len(rec.installments)} installment(s)): {premium_total}",
        f"Term {rec.term_start} to {rec.term_end}: {term_days} days; cancellation "
        f"effective {effective_date}: {elapsed} days earned, {unearned_days} days unearned",
        f"Earned premium (pro-rata): {premium_total} × {elapsed}/{term_days} = {earned}",
        f"Paid to date (installments, billed basis): {paid_to_date}",
    ]
    if return_premium > ZERO:
        computation.append(
            f"Return premium (paid − earned): {paid_to_date} − {earned} = {return_premium}, "
            f"owed to the {payee_kind} ({payee_name})"
        )
    elif insured_owes > ZERO:
        computation.append(
            f"Paid {paid_to_date} is behind earned {earned}: the insured owes "
            f"{insured_owes}; no return premium is due and no payable opens"
        )
    else:
        computation.append("Paid equals earned to the cent: no return premium is due")

    cxl = Cancellation(
        cancellation_id=f"CXL-{policy_number}",
        policy_number=policy_number,
        effective_date=effective_date,
        recorded_on=recorded_on,
        recorded_by=recorded_by,
        reason=reason,
        premium_total=cents(premium_total),
        term_days=term_days,
        elapsed_days=elapsed,
        earned_premium=earned,
        paid_to_date=cents(paid_to_date),
        return_premium=cents(return_premium),
        insured_owes=cents(insured_owes),
        payee_kind=payee_kind,
        payee_name=payee_name,
        broker_id=rec.broker_id,
        payable_id=payable_id,
        computation=computation,
    )
    rec.status = PolicyStatus.CANCELLED
    store.upsert_receivable(rec)
    store.add_cancellation(cxl)
    store.audit(AuditEvent(
        at=_now(), actor=recorded_by, action="policy_cancelled", subject=policy_number,
        detail=f"Cancelled effective {effective_date}"
               + (f" ({reason})" if reason else "")
               + f": earned {earned} of {premium_total}, paid {paid_to_date}, "
               f"return premium {return_premium}."))

    if payable_id is not None:
        payable = ReturnPremiumPayable(
            payable_id=payable_id,
            policy_number=policy_number,
            cancellation_id=cxl.cancellation_id,
            payee_kind=payee_kind,
            payee_name=payee_name,
            broker_id=rec.broker_id,
            amount=cents(return_premium),
            opened_on=recorded_on,
            events=[PayableEvent(
                on=recorded_on, kind="opened", amount=cents(return_premium),
                ref=cxl.cancellation_id,
                note=f"Return premium on {policy_number}, owed to the {payee_kind}.",
            )],
        )
        store.save_payable(payable)
        store.audit(AuditEvent(
            at=_now(), actor=recorded_by, action="payable_opened", subject=payable_id,
            detail=f"Return premium payable of {return_premium} opened on {policy_number}, "
                   f"owed to the {payee_kind} ({payee_name})."))
    return cxl
