"""The month-end close pack: the period control, as an export.

A day is judged by its footing; a month is judged by its close pack.
The pack is a read model over the same books the daily reports use —
assembling it changes nothing, and closing a month here is a report,
not a lock: the books stay open, and a pack regenerated later from
unchanged books is byte-identical.

The control roll runs over the suspense ledger, because suspense is
where unapplied cash lives:

    opening suspense + cash received into suspense in the month
    − applied from suspense in the month
    − returned from suspense in the month = closing suspense

The last term is the bank taking cash back: a returned line's
suspense leaves by return, not by application, and the pack carries
the month's returns (count and total) as a control line of their
own, with each return listed as a reversing entry.

Each suspense item's opening position is reconstructed from the
records (the amount that entered suspense when its bank line was
worked, less what the re-match packs have since released), so the
closing figure can be cross-checked against the ledger itself. If
the two disagree, or an item cannot be traced to a bank line, the
pack says NOT FOOTED in words and names the difference. Approved
write-offs stand in the roll as their own control line: they dispose
of receivable deltas (a partial shortfall, a netting delta), not of
suspense cash, so they are reported in the pack — with the packs and
approvers that carried them — rather than netted into the suspense
identity where they do not belong.

Return premiums get the same treatment, on their own ledger. A
payable is money the carrier owes back, not cash it holds, so the
pack rolls the payable ledger on its own identity — opening
payables + opened in the month − netted against incoming cash
− paid out under approved payout packs = closing payables — with
each position reconstructed from the payables' own chains at the
month boundaries, its own footed flag, and its own discrepancies.
The only place the two ledgers ever meet is the day: cash netted
against a payable is a named term of the day report's footing, and
the month's netted total below is that term summed.

The pack body contains no wall-clock stamps. Its SHA-256 digest is
computed over the canonical body, so the exported JSON and the
human-readable text rendering are two views of one record.

The suspense identity holds PER CURRENCY: on a book that holds
more than one, the roll runs in each currency separately — opening
+ received − applied − returned = closing in EUR and in USD alike,
never a blended figure — and the headline roll is the functional
currency's (USD). Beside the rolls, the pack carries functional
equivalents of the closing suspense at month-end governed rates,
labelled as translations: they present the position in one
currency for a reader; they are not postings, and no control in
the pack runs on them.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .fx import FUNCTIONAL_CURRENCY, rate_on, to_functional
from .models import ReturnPremiumPayable
from .money import ZERO, as_str
from .nonpremium import non_premium_ledger
from .report import BUCKETS, broker_scorecard, proposal_quality
from .store import Store


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def parse_month(text: str) -> tuple[int, int]:
    parts = text.split("-")
    if len(parts) != 2:
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    return year, month


class _Position:
    """One suspense item, reconstructed: what entered suspense when
    its bank line was worked, and what re-match packs have released
    since, by the packs' value dates. A bank return of the item's
    line releases its suspense share on the return date — the whole
    remaining position for a full return, exactly the recorded
    share for a part-return (suspense unwinds first, so the share
    is the part of the position the return took) — the cash went
    back to the payer, so it is in suspense no longer, and the roll
    names that as its own term (returned_from_suspense) rather than
    folding it into applications. A return recorded before
    part-returns existed carries no stored share: it reads as the
    whole remaining position on its date, exactly as the position
    was read before shares were recorded."""

    def __init__(self, item, created: Decimal | None, releases: list[tuple[date, Decimal]],
                 return_events: list[tuple[date, Decimal | None]] | None = None):
        self.item = item
        self.created = created  # None when the bank line is not on the books
        self.releases = sorted(releases)
        # (return date, suspense share) in date order; a None share
        # is a legacy full return: the whole remaining position.
        self.return_events = sorted(return_events or [])

    @property
    def returned_on(self) -> date | None:
        """The line's first return date, if any."""
        return self.return_events[0][0] if self.return_events else None

    def _return_release_at(self, upto: date) -> Decimal:
        """What the line's returns dated on or before `upto` took
        out of this position, walking the returns in order: an
        explicit share subtracts itself; a legacy return takes
        whatever of the position still stood on its date."""
        taken = ZERO
        for d, share in self.return_events:
            if d > upto:
                break
            if share is None:
                released = sum((amt for rd, amt in self.releases if rd <= d), ZERO)
                taken = self.created - released if self.created is not None else taken
            else:
                taken += share
        return taken

    def open_at(self, as_of: date) -> Decimal:
        if self.created is None:
            return self.item.amount
        for d, share in self.return_events:
            if share is None and d <= as_of:
                # A legacy full return: the position closed on its
                # date, exactly as it read before shares existed.
                return ZERO
        released = sum((amt for d, amt in self.releases if d <= as_of), ZERO)
        return self.created - released - self._return_release_at(as_of)

    def released_by_return(self) -> Decimal:
        """The amount the line's returns released in total: what was
        still open on each return date, taken in order. Zero when
        the line was never returned."""
        if not self.return_events or self.created is None:
            return ZERO
        return self._return_release_at(self.return_events[-1][0])

    def returned_between(self, first: date, last: date) -> Decimal:
        """The suspense this position's returns released with return
        dates inside [first, last] — the roll's returned term for a
        period, per position."""
        before = self._return_release_at(first - timedelta(days=1))
        return self._return_release_at(last) - before


def _positions(store: Store) -> tuple[list[_Position], dict[str, Decimal]]:
    lines = {l.bank_line_id: l for l in store.bank_lines()}
    return_events: dict[str, list[tuple[date, Decimal | None]]] = defaultdict(list)
    for r in store.returns():
        return_events[r.bank_line_id].append((r.return_date, r.suspense_released))
    day_alloc: dict[str, Decimal] = defaultdict(lambda: ZERO)
    releases: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for pack in store.packs():
        for entry in pack.entries:
            if not entry.allocation:
                continue
            if pack.pack_id.startswith("PACK-REMATCH"):
                releases[entry.allocation.bank_line_id].append(
                    (pack.value_date, entry.allocation.amount)
                )
            elif pack.pack_id.startswith("PACK-COMB"):
                # Combination packs release suspense like re-match
                # packs — their legs were suspense cash until the
                # pair was proposed — except that a rejected
                # combination frees its legs (the gate re-opens
                # their items), so a rejected pack releases nothing.
                if pack.status.value != "rejected":
                    releases[entry.allocation.bank_line_id].append(
                        (pack.value_date, entry.allocation.amount)
                    )
            else:
                day_alloc[entry.allocation.bank_line_id] += entry.allocation.amount
    positions = []
    for item in store.suspense_items():
        line = lines.get(item.bank_line_id)
        created = None if line is None else line.amount - day_alloc.get(item.bank_line_id, ZERO)
        positions.append(_Position(item, created, releases.get(item.bank_line_id, []),
                                   return_events.get(item.bank_line_id)))
    return positions, lines


def _payable_remaining_at(payable: ReturnPremiumPayable, cutoff: date) -> Decimal:
    """A return-premium payable's standing at a date, reconstructed
    from its own chain: the opened amount less every netting and
    payout dated on or before the cutoff. A payable opened after
    the cutoff was not owed yet and contributes nothing."""
    if payable.opened_on > cutoff:
        return ZERO
    moved = sum(
        (e.amount for e in payable.events
         if e.kind in ("netted", "paid") and e.on <= cutoff),
        ZERO,
    )
    return payable.amount - moved


def _payable_roll(store: Store, first: date, last: date) -> dict:
    """The payable ledger's own roll for the month: positions at the
    boundaries reconstructed from the chains, movements from the
    chains' dated events, and the identity checked in the open."""
    day_before = first - timedelta(days=1)
    payables = store.payables()
    opening = sum((_payable_remaining_at(p, day_before) for p in payables), ZERO)
    closing = sum((_payable_remaining_at(p, last) for p in payables), ZERO)
    opened = sum((p.amount for p in payables if first <= p.opened_on <= last), ZERO)
    netted = sum(
        (e.amount for p in payables for e in p.events
         if e.kind == "netted" and first <= e.on <= last),
        ZERO,
    )
    paid = sum(
        (e.amount for p in payables for e in p.events
         if e.kind == "paid" and first <= e.on <= last),
        ZERO,
    )
    footed = opening + opened - netted - paid == closing
    discrepancies: list[dict] = []
    if not footed:
        discrepancies.append({
            "problem": f"the payable ledger does not foot: opening {as_str(opening)} "
                       f"+ opened {as_str(opened)} − netted {as_str(netted)} "
                       f"− paid {as_str(paid)} != closing {as_str(closing)}",
        })
    payout_packs = [
        {
            "pack_id": p.pack_id,
            "value_date": p.value_date.isoformat(),
            "total": as_str(p.total),
            "payables": len(p.entries),
            "approvals": list(p.approvals),
            "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        }
        for p in sorted(store.payout_packs(), key=lambda pk: pk.pack_id)
        if p.status.value == "approved" and first <= p.value_date <= last
    ]
    return {
        "opening_payables": as_str(opening),
        "payables_opened_in_month": as_str(opened),
        "netted_in_month": as_str(netted),
        "paid_in_month": as_str(paid),
        "closing_payables": as_str(closing),
        "footed": footed,
        "discrepancies": discrepancies,
        "open_payables_at_close": [
            {
                "payable_id": p.payable_id,
                "policy_number": p.policy_number,
                "payee": f"{p.payee_kind}: {p.payee_name}",
                "remaining": as_str(_payable_remaining_at(p, last)),
            }
            for p in payables
            if _payable_remaining_at(p, last) > ZERO
        ],
        "payout_packs_approved_in_month": payout_packs,
        "note": (
            "Return premiums are money owed back to insureds and brokers, not "
            "cash held: this ledger rolls on its own identity and never "
            "enters the suspense roll above."
        ),
    }


def _aging(positions: list[_Position], as_of: date) -> list[dict]:
    """Suspense open at `as_of`, bucketed like the daily report, with
    each item's reconstructed open amount."""
    members = []
    for pos in positions:
        amount = pos.open_at(as_of)
        if amount > 0 and pos.item.received_date <= as_of:
            members.append((pos, amount))
    out = []
    for low, high, label in BUCKETS:
        bucket = [
            (pos, amount) for pos, amount in members
            if pos.item.age_days(as_of) >= low
            and (high is None or pos.item.age_days(as_of) <= high)
        ]
        bucket.sort(key=lambda pa: pa[0].item.received_date)
        totals_by_currency: dict[str, Decimal] = {}
        for pos, amount in bucket:
            ccy = pos.item.currency
            totals_by_currency[ccy] = totals_by_currency.get(ccy, ZERO) + amount
        out.append({
            "bucket": label,
            "total": as_str(sum((amount for _p, amount in bucket), ZERO)),
            "totals_by_currency": {
                ccy: as_str(total) for ccy, total in sorted(totals_by_currency.items())
            },
            "count": len(bucket),
            "items": [
                {
                    "suspense_id": pos.item.suspense_id,
                    "bank_line_id": pos.item.bank_line_id,
                    "amount": as_str(amount),
                    "currency": pos.item.currency,
                    "received_date": pos.item.received_date.isoformat(),
                    "age_days": pos.item.age_days(as_of),
                    "reason": pos.item.reason,
                }
                for pos, amount in bucket
            ],
        })
    return out


def build_close_pack(store: Store, year: int, month: int) -> dict:
    """Assemble the close pack body for a calendar month. Pure read:
    nothing in the books changes."""
    first, last = month_bounds(year, month)
    day_before = first - timedelta(days=1)
    positions, lines = _positions(store)

    def in_month(d: date) -> bool:
        return first <= d <= last

    discrepancies: list[dict] = []
    for pos in positions:
        if pos.created is None:
            discrepancies.append({
                "suspense_id": pos.item.suspense_id,
                "amount": as_str(pos.item.amount),
                "problem": f"suspense item {pos.item.suspense_id} names bank line "
                           f"{pos.item.bank_line_id}, which is not on the books",
            })
        elif pos.item.status == "open" and all(d <= last for d, _a in pos.releases):
            reconstructed = pos.open_at(last)
            if pos.item.received_date <= last and reconstructed != pos.item.amount:
                discrepancies.append({
                    "suspense_id": pos.item.suspense_id,
                    "amount": as_str(abs(reconstructed - pos.item.amount)),
                    "problem": f"suspense item {pos.item.suspense_id}: the ledger holds "
                               f"{as_str(pos.item.amount)} but the records reconstruct "
                               f"{as_str(reconstructed)}",
                })

    # The suspense identity runs per currency: each currency's
    # positions roll on their own — opening + received − applied
    # − returned = closing, in that currency — and the headline
    # roll is the functional currency's. On a single-currency book
    # the headline roll is the whole roll, exactly as before.
    def _roll(subset: list[_Position]) -> dict:
        r_opening = sum(
            (pos.open_at(day_before) for pos in subset if pos.item.received_date < first),
            ZERO,
        )
        r_received = sum(
            ((pos.created if pos.created is not None else pos.item.amount)
             for pos in subset if in_month(pos.item.received_date)),
            ZERO,
        )
        r_applied = sum(
            (amt for pos in subset for d, amt in pos.releases if in_month(d)),
            ZERO,
        )
        r_returned = sum(
            (pos.returned_between(first, last) for pos in subset),
            ZERO,
        )
        r_closing = sum(
            (pos.open_at(last) for pos in subset if pos.item.received_date <= last),
            ZERO,
        )
        r_roll_closing = r_opening + r_received - r_applied - r_returned
        return {
            "opening": r_opening, "received": r_received, "applied": r_applied,
            "returned": r_returned, "closing": r_closing,
            "roll_closing": r_roll_closing, "difference": r_roll_closing - r_closing,
        }

    item_currency = {pos.item.suspense_id: pos.item.currency for pos in positions}
    currencies_present = sorted(
        {pos.item.currency for pos in positions}
        | {l.currency for l in lines.values() if in_month(l.value_date)}
    )
    rolls: dict[str, dict] = {}
    for ccy in currencies_present:
        subset = [pos for pos in positions if pos.item.currency == ccy]
        roll = _roll(subset)
        ccy_discrepancies = [
            d for d in discrepancies
            if item_currency.get(d.get("suspense_id")) == ccy
        ]
        roll["footed"] = roll["difference"] == 0 and not ccy_discrepancies
        rolls[ccy] = roll
    headline = rolls.get(FUNCTIONAL_CURRENCY) or _roll([])
    opening = headline["opening"]
    received = headline["received"]
    applied = headline["applied"]
    returned_from_suspense = headline["returned"]
    closing = headline["closing"]
    roll_closing = headline["roll_closing"]
    difference = headline["difference"]

    month_returns = [r for r in store.returns() if in_month(r.return_date)]

    # The month's packs: value date in the month and standing
    # approved. (A September pack approved on October the first is
    # part of September's close; its decided_at is printed on the
    # row, so a late approval is visible, never hidden.)
    packs_approved = []
    write_offs = ZERO
    applied_approved = ZERO
    for pack in sorted(store.packs(), key=lambda p: p.pack_id):
        if pack.status.value != "approved" or pack.decided_at is None:
            continue
        if not in_month(pack.value_date):
            continue
        packs_approved.append({
            "pack_id": pack.pack_id,
            "value_date": pack.value_date.isoformat(),
            "assembled_by": pack.assembled_by,
            "approvals": list(pack.approvals),
            "allocation_total": as_str(pack.allocation_total),
            "write_off_total": as_str(pack.write_off_total),
            "decided_at": pack.decided_at.isoformat(),
            "flags": list(pack.flags),
        })
        write_offs += pack.write_off_total
        applied_approved += pack.allocation_total

    cash_received = sum(
        (l.amount for l in lines.values()
         if in_month(l.value_date) and l.currency == FUNCTIONAL_CURRENCY), ZERO
    )
    cash_received_by_currency: dict[str, Decimal] = {}
    for l in lines.values():
        if in_month(l.value_date):
            cash_received_by_currency[l.currency] = (
                cash_received_by_currency.get(l.currency, ZERO) + l.amount)

    def exc_view(e):
        return {
            "exception_id": e.exception_id,
            "code": e.code.value,
            "bank_line_id": e.bank_line_id,
            "policy_number": e.policy_number,
            "broker_id": e.broker_id,
            "amount": as_str(e.amount),
            "currency": e.currency,
            "disposition": e.status,
            "first_seen": e.first_seen.isoformat(),
            "detail": e.detail,
        }

    all_exceptions = sorted(store.exceptions(), key=lambda e: e.exception_id)
    opened = [exc_view(e) for e in all_exceptions if in_month(e.first_seen)]
    closed = [
        exc_view(e) for e in all_exceptions
        if e.status != "open" and e.first_seen <= last
    ]

    footed = all(roll["footed"] for roll in rolls.values()) if rolls else (
        difference == 0 and not discrepancies
    )
    multi_currency = len(currencies_present) > 1
    if footed and not multi_currency:
        footing_note = (
            "The month FOOTS: opening suspense + cash received into suspense "
            "− applied from suspense"
            + (" − returned from suspense" if returned_from_suspense > ZERO else "")
            + " = closing suspense, to the cent, and the "
            "closing figure agrees with the suspense ledger."
        )
    elif footed:
        footing_note = (
            "The month FOOTS in each currency: opening suspense + cash "
            "received into suspense − applied from suspense − returned from "
            "suspense = closing suspense, per currency, to the cent, and "
            "each currency's closing figure agrees with the suspense "
            "ledger. The functional figures below are translations at "
            "month-end rates — presentation, not postings."
        )
    elif not multi_currency:
        bits = []
        if difference != 0:
            bits.append(f"the control roll misses the ledger by {as_str(difference)}")
        bits += [d["problem"] for d in discrepancies]
        footing_note = "NOT FOOTED: " + "; ".join(bits) + "."
    else:
        bits = [
            f"in {ccy} the control roll misses the ledger by "
            f"{as_str(roll['difference'])}"
            for ccy, roll in sorted(rolls.items()) if roll["difference"] != 0
        ]
        bits += [d["problem"] for d in discrepancies]
        footing_note = "NOT FOOTED: " + "; ".join(bits) + "."

    # Functional equivalents of the closing position, at the
    # governed rates in force on the month's last day. Translations
    # for presentation only — labelled as such, never posted, and
    # no control in this pack runs on them.
    month_end_rates = {
        ccy: str(rate_on(ccy, last))
        for ccy in currencies_present if ccy != FUNCTIONAL_CURRENCY
    }
    closing_translated = {
        ccy: as_str(to_functional(roll["closing"], ccy, last))
        for ccy, roll in sorted(rolls.items())
    }
    functional_section = {
        "currency": FUNCTIONAL_CURRENCY,
        "month_end_rates": month_end_rates,
        "closing_suspense_translated": closing_translated,
        "closing_suspense_functional_total": as_str(sum(
            (Decimal(v) for v in closing_translated.values()), ZERO)),
        "note": (
            "Translations at the governed rates in force on the month's "
            "last day, for presentation only: they are not postings, and "
            "no control in this pack runs on a translated figure. The "
            "suspense identity above holds per currency, in the currency."
        ),
    }
    returns_by_currency: dict[str, Decimal] = {}
    for r in month_returns:
        returns_by_currency[r.currency] = (
            returns_by_currency.get(r.currency, ZERO) + r.amount)

    body = {
        "month": f"{year:04d}-{month:02d}",
        "period": {"first": first.isoformat(), "last": last.isoformat()},
        "control_roll": {
            "opening_suspense": as_str(opening),
            "opening_aging": _aging(
                [p for p in positions if p.item.received_date < first], day_before
            ),
            "cash_received_in_month": as_str(cash_received),
            "cash_received_in_month_by_currency": {
                ccy: as_str(total)
                for ccy, total in sorted(cash_received_by_currency.items())
            },
            "received_into_suspense": as_str(received),
            "applied_from_suspense": as_str(applied),
            "returned_from_suspense": as_str(returned_from_suspense),
            "returns_in_month": {
                "count": len(month_returns),
                "total": as_str(sum((r.amount for r in month_returns), ZERO)),
                "totals_by_currency": {
                    ccy: as_str(total)
                    for ccy, total in sorted(returns_by_currency.items())
                },
                "partial_count": len([r for r in month_returns if r.partial]),
                "partial_total": as_str(
                    sum((r.amount for r in month_returns if r.partial), ZERO)),
            },
            "by_currency": {
                ccy: {
                    "opening_suspense": as_str(roll["opening"]),
                    "received_into_suspense": as_str(roll["received"]),
                    "applied_from_suspense": as_str(roll["applied"]),
                    "returned_from_suspense": as_str(roll["returned"]),
                    "closing_suspense": as_str(roll["closing"]),
                    "roll_closing_suspense": as_str(roll["roll_closing"]),
                    "difference": as_str(roll["difference"]),
                    "footed": roll["footed"],
                }
                for ccy, roll in sorted(rolls.items())
            },
            "applied_allocations_approved": as_str(applied_approved),
            "approved_write_offs": as_str(write_offs),
            "closing_suspense": as_str(closing),
            "roll_closing_suspense": as_str(roll_closing),
            "difference": as_str(difference),
            "footed": footed,
            "footing_note": footing_note,
        },
        "functional": functional_section,
        "return_premiums": _payable_roll(store, first, last),
        "payment_returns_in_month": [
            {
                "return_id": r.return_id,
                "bank_line_id": r.bank_line_id,
                "reason": r.reason.value,
                "return_date": r.return_date.isoformat(),
                "amount": as_str(r.amount),
                "currency": r.currency,
                "partial": r.partial,
                "recorded_by": r.recorded_by,
                "pack_ids": list(r.pack_ids),
                "detail": r.detail,
            }
            for r in month_returns
        ],
        "packs_approved_in_month": packs_approved,
        "exceptions_opened_in_month": opened,
        "exceptions_closed_by_month_end": closed,
        "suspense_aging_at_month_end": _aging(positions, last),
        "broker_scorecard": broker_scorecard(store, last, month=(year, month)),
        "proposal_quality": proposal_quality(store, month=(year, month)),
        "combination_settlements": _combination_section(store, first, last),
        "non_premium": non_premium_ledger(store, month=(year, month)),
        "discrepancies": discrepancies,
    }
    return body


def _combination_section(store: Store, first: date, last: date) -> dict:
    """The month's combination settlements, distinct from ordinary
    allocations: pairs of same-payer wires that together settled
    one open installment exactly, each itemised with both legs.
    `settled_total` counts approved packs — money that actually
    applied; proposed combinations are listed with their status so
    a pair still waiting at the gate is visible, not silently
    inside or outside the number. The suspense releases these
    packs made are already inside the control roll's
    applied-from-suspense term; this section is the explanation,
    not a second counting."""
    items: list[dict] = []
    settled_total = ZERO
    for pack in sorted(store.packs(), key=lambda p: p.pack_id):
        if not pack.pack_id.startswith("PACK-COMB"):
            continue
        if not (first <= pack.value_date <= last):
            continue
        by_comb: dict[str, dict] = {}
        for entry in pack.entries:
            if entry.combination_id and entry.allocation:
                slot = by_comb.setdefault(entry.combination_id, {
                    "combination_id": entry.combination_id,
                    "pack_id": pack.pack_id,
                    "status": pack.status.value,
                    "policy_number": entry.allocation.policy_number,
                    "installment_id": entry.allocation.installment_id,
                    "currency": entry.allocation.currency,
                    "legs": [],
                    "cash_total": ZERO,
                })
                slot["legs"].append({
                    "bank_line_id": entry.allocation.bank_line_id,
                    "amount": as_str(entry.allocation.amount),
                })
                slot["cash_total"] += entry.allocation.amount
        for slot in by_comb.values():
            if slot["status"] == "approved":
                settled_total += slot["cash_total"]
            slot["cash_total"] = as_str(slot["cash_total"])
            items.append(slot)
    return {
        "count": len(items),
        "settled_total": as_str(settled_total),
        "items": items,
    }


def pack_digest(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def close_pack_export(store: Store, year: int, month: int) -> dict:
    """The exportable pack: the body, its digest, and a generated-as
    header that stays outside the hashed body."""
    body = build_close_pack(store, year, month)
    return {
        "month": body["month"],
        "generated_as_of": body["period"]["last"],
        "sha256": pack_digest(body),
        "pack": body,
    }


def render_close_pack_text(export: dict) -> str:
    body = export["pack"]
    roll = body["control_roll"]
    out = [
        f"MONTH-END CLOSE PACK — {body['month']}",
        f"Generated as of {export['generated_as_of']} · SHA-256 {export['sha256']}",
        "",
        "CONTROL ROLL (suspense ledger)",
        f"  Opening suspense:            {roll['opening_suspense']:>12}",
        f"  Cash received in month:      {roll['cash_received_in_month']:>12}  (bank total, all lines)",
        f"  Received into suspense:      {roll['received_into_suspense']:>12}",
        f"  Applied from suspense:       {roll['applied_from_suspense']:>12}",
        f"  Returned from suspense:      {roll['returned_from_suspense']:>12}  (cash the bank took back; it leaves suspense by return, not by application)",
        f"  Payment returns in month:    {roll['returns_in_month']['count']:>12}  totalling {roll['returns_in_month']['total']} (reversing entries below"
        + (f"; {roll['returns_in_month']['partial_count']} of them part-returns totalling {roll['returns_in_month']['partial_total']}"
           if roll['returns_in_month'].get('partial_count') else "") + ")",
        f"  Applied via approved packs:  {roll['applied_allocations_approved']:>12}",
        f"  Approved write-offs:         {roll['approved_write_offs']:>12}  (receivable deltas; they do not release suspense cash)",
        f"  Closing suspense:            {roll['closing_suspense']:>12}",
        f"  {roll['footing_note']}",
    ]
    by_currency = roll.get("by_currency") or {}
    if len(by_currency) > 1:
        out += ["", "CONTROL ROLL BY CURRENCY (the identity holds in each currency)"]
        for ccy, r in by_currency.items():
            out.append(
                f"  {ccy}: opening {r['opening_suspense']} + received into suspense "
                f"{r['received_into_suspense']} − applied {r['applied_from_suspense']} "
                f"− returned {r['returned_from_suspense']} = closing "
                f"{r['closing_suspense']} — {'FOOTS' if r['footed'] else 'NOT FOOTED'}"
            )
        cash_by_ccy = roll.get("cash_received_in_month_by_currency") or {}
        out.append(
            "  Cash received in month by currency: "
            + ", ".join(f"{ccy} {total}" for ccy, total in cash_by_ccy.items())
            + "  (the headline roll above is the functional currency's)"
        )
        functional = body.get("functional") or {}
        if functional.get("month_end_rates"):
            rates = ", ".join(
                f"{ccy} {rate}" for ccy, rate in functional["month_end_rates"].items())
            translated = ", ".join(
                f"{ccy} {amount}"
                for ccy, amount in functional["closing_suspense_translated"].items())
            out += [
                "",
                "FUNCTIONAL TRANSLATIONS (presentation only — not postings)",
                f"  Month-end governed rates: {rates}",
                f"  Closing suspense translated: {translated}",
                f"  Closing suspense, functional total: "
                f"{functional['closing_suspense_functional_total']}",
            ]
    out += [
        "",
        f"POSTING PACKS APPROVED IN THE MONTH ({len(body['packs_approved_in_month'])})",
    ]
    for pack in body["packs_approved_in_month"]:
        out.append(
            f"  {pack['pack_id']} · value {pack['value_date']} · assembled by "
            f"{pack['assembled_by']} · approved by {', '.join(pack['approvals'])} · "
            f"allocations {pack['allocation_total']} · write-offs {pack['write_off_total']}"
            + (f" · flags: {', '.join(pack['flags'])}" if pack.get("flags") else "")
        )
    rp = body["return_premiums"]
    if (
        rp["opening_payables"] != "0.00" or rp["closing_payables"] != "0.00"
        or rp["payables_opened_in_month"] != "0.00" or rp["netted_in_month"] != "0.00"
        or rp["paid_in_month"] != "0.00" or rp["payout_packs_approved_in_month"]
    ):
        out += [
            "",
            "RETURN PREMIUMS (payable ledger — money owed back, not cash held)",
            f"  Opening payables:            {rp['opening_payables']:>12}",
            f"  Opened in month:             {rp['payables_opened_in_month']:>12}",
            f"  Netted against cash:         {rp['netted_in_month']:>12}",
            f"  Paid out (approved packs):   {rp['paid_in_month']:>12}",
            f"  Closing payables:            {rp['closing_payables']:>12}",
            "  The payable ledger FOOTS on its own identity."
            if rp["footed"] else "  The payable ledger does NOT FOOT — see discrepancies.",
        ]
        for od in rp["open_payables_at_close"]:
            out.append(
                f"    {od['payable_id']} · {od['policy_number']} · owed to "
                f"{od['payee']} · {od['remaining']:>10} still standing"
            )
        for pp in rp["payout_packs_approved_in_month"]:
            out.append(
                f"    Payout pack {pp['pack_id']} · value {pp['value_date']} · "
                f"{pp['total']:>10} across {pp['payables']} payable(s) · approved by "
                f"{', '.join(pp['approvals'])}"
            )
    if body["payment_returns_in_month"]:
        out += ["", f"PAYMENT RETURNS IN THE MONTH ({len(body['payment_returns_in_month'])})"]
        for r in body["payment_returns_in_month"]:
            out.append(
                f"  {r['return_id']} · {r['bank_line_id']} · {r['reason']} · "
                f"{r['amount']:>10} · returned {r['return_date']} · recorded by "
                f"{r['recorded_by']} · reversing packs: {', '.join(r['pack_ids']) or '—'}"
                + (" · PART-RETURN: the rest of the line stands" if r.get("partial") else "")
            )
    out += ["", f"EXCEPTIONS OPENED IN THE MONTH ({len(body['exceptions_opened_in_month'])})"]
    for e in body["exceptions_opened_in_month"]:
        out.append(
            f"  {e['code']:<22} {e['exception_id']:<46} {e['amount']:>10}  "
            f"[{e['disposition']}] {e['detail']}"
        )
    out += ["", f"EXCEPTIONS CLOSED BY MONTH END ({len(body['exceptions_closed_by_month_end'])})"]
    for e in body["exceptions_closed_by_month_end"]:
        out.append(
            f"  {e['code']:<22} {e['exception_id']:<46} {e['amount']:>10}  [{e['disposition']}]"
        )
    out += ["", "SUSPENSE AGING AT MONTH END"]
    for bucket in body["suspense_aging_at_month_end"]:
        out.append(f"  {bucket['bucket']:<10} {bucket['total']:>10}  ({bucket['count']} items)")
        for item in bucket["items"]:
            out.append(
                f"      {item['bank_line_id']:<10} {item['amount']:>10}  "
                f"{item['age_days']} days — {item['reason']}"
            )
    combos = body.get("combination_settlements") or {"count": 0, "settled_total": "0.00", "items": []}
    if combos["count"]:
        out += ["", f"COMBINATION SETTLEMENTS ({combos['count']}) — settled via approved pairs: {combos['settled_total']}"]
        for c in combos["items"]:
            legs = " + ".join(f"{leg['bank_line_id']} {leg['amount']}" for leg in c["legs"])
            out.append(
                f"  {c['combination_id']} · {legs} → {c['installment_id']} · "
                f"cash {c['cash_total']} · [{c['status']}] in {c['pack_id']}"
            )
    non_premium = body.get("non_premium") or {"posted_total": "0.00", "by_kind": {}, "items": []}
    if non_premium["items"]:
        out += ["", f"NON-PREMIUM DISPOSITIONS — posted to the non-premium ledger: {non_premium['posted_total']}"]
        for kind, slot in non_premium["by_kind"].items():
            out.append(
                f"  {kind:<18} {slot['count']} line(s) · approved {slot['approved']} · "
                f"proposed {slot['proposed']} · rejected {slot['rejected']}"
            )
        out.append(
            "  Non-premium cash never enters suspense and never touches a "
            "receivable; it is a term of the days' footing, not of this roll."
        )
    quality = body.get("proposal_quality") or {"by_kind": {}, "totals": {"proposals": 0}}
    if quality["totals"]["proposals"]:
        out += ["", "MODEL PROPOSAL QUALITY (packs decided with this month's value dates)"]
        for kind, s in quality["by_kind"].items():
            out.append(
                f"  {kind:<18} proposals {s['proposals']} · accepted as proposed "
                f"{s['accepted_as_proposed']} · accepted with edits {s['accepted_with_edits']} · "
                f"rejected {s['rejected']} · value edited {s['value_edited']}"
            )
        t = quality["totals"]
        out.append(
            f"  {'ALL':<18} proposals {t['proposals']} · accepted as proposed "
            f"{t['accepted_as_proposed']} · accepted with edits {t['accepted_with_edits']} · "
            f"rejected {t['rejected']} · value edited {t['value_edited']}"
        )
    out += ["", "BROKER SCORECARD FOR THE MONTH"]
    for row in body["broker_scorecard"]:
        rate = row["clean_netting_rate"] or "—"
        out.append(
            f"  {row['broker']:<34} bank {row['bank_total']:>10}  allocated "
            f"{row['allocated_total']:>10}  suspense {row['suspense_total']:>9}  "
            f"clean netting {rate}  mismatches {row['netting_mismatches']}"
        )
    if body["discrepancies"]:
        out += ["", "DISCREPANCIES"]
        for d in body["discrepancies"]:
            out.append(f"  {d['suspense_id']}: {d['problem']} (amount at issue {d['amount']})")
    out += [
        "",
        "Closing a month here is a report, not a lock: the books stay open, "
        "and this pack regenerates byte-identically from unchanged books.",
    ]
    return "\n".join(out) + "\n"
