"""The daily reconciliation report — the control the day is judged by.

One question, answered to the cent: does the bank total equal what was
applied plus what sits in suspense? If the footing fails, the report
says so in words and refuses to call the day reconciled. Exceptions
are listed, never netted away; suspense is aged in buckets with the
oldest items named, because aged unapplied cash is what auditors ask
about first.

The footing runs PER CURRENCY. A day that takes both USD and EUR cash
foots in USD and in EUR separately — no figure in the report adds one
currency to another — and the report also carries a functional (USD)
translation of the day, at the governed rates in force on each line's
value date, labelled as what it is: a reporting conversion, not a
posting and not the control.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import Policy as PolicyConfig
from .fx import FUNCTIONAL_CURRENCY, rate_on, to_functional
from .models import ExceptionCode, Tier
from .money import ZERO, cents
from .store import Store

BUCKETS = [(0, 7, "0–7 days"), (8, 30, "8–30 days"), (31, 60, "31–60 days"), (61, None, "60+ days")]


def non_premium_view(store: Store, day: date) -> dict:
    """The day's non-premium dispositions: lines the classifier
    routed out of the premium pipeline, standing in their NPACK
    disposition packs. These amounts are a term of the day's
    footing — the cash is in the bank total, but it is not premium
    and never enters suspense — and the pack's status says how far
    the gate has taken them."""
    items: list[dict] = []
    total = ZERO
    for pack in store.packs(on=day):
        if not pack.pack_id.startswith("NPACK-"):
            continue
        for entry in pack.entries:
            if entry.non_premium:
                np = entry.non_premium
                total += np.amount
                items.append({
                    "bank_line_id": np.bank_line_id,
                    "kind": np.kind,
                    "amount": str(np.amount),
                    "currency": np.currency,
                    "payer_name": np.payer_name,
                    "pack_id": pack.pack_id,
                    "status": pack.status.value,
                })
    return {"total": str(total), "count": len(items), "items": items}


def combination_settlements(store: Store, day: date) -> dict:
    """Combination settlements proposed on this day, as their own
    section: pairs of same-payer wires whose cash together settles
    one open installment exactly, standing in PACK-COMB packs at
    the gate. Each combination is itemised leg by leg — the money
    is two payers' wires no longer, but it is still two bank lines,
    and the report keeps both names on it."""
    prefix = f"PACK-COMB-{day.isoformat()}"
    combos: list[dict] = []
    total = ZERO
    for pack in store.packs():
        if pack.pack_id != prefix and not pack.pack_id.startswith(prefix + "-"):
            continue
        by_comb: dict[str, dict] = {}
        for entry in pack.entries:
            if entry.combination_id and entry.allocation:
                slot = by_comb.setdefault(entry.combination_id, {
                    "combination_id": entry.combination_id,
                    "policy_number": entry.allocation.policy_number,
                    "installment_id": entry.allocation.installment_id,
                    "currency": entry.allocation.currency,
                    "pack_id": pack.pack_id,
                    "status": pack.status.value,
                    "legs": [],
                    "cash_total": ZERO,
                })
                slot["legs"].append({
                    "bank_line_id": entry.allocation.bank_line_id,
                    "amount": str(entry.allocation.amount),
                })
                slot["cash_total"] += entry.allocation.amount
        for slot in by_comb.values():
            slot["cash_total"] = str(slot["cash_total"])
            total += sum((Decimal(leg["amount"]) for leg in slot["legs"]), ZERO)
            combos.append(slot)
    return {"total": str(total), "count": len(combos), "items": combos}


def rematch_recoveries(store: Store, day: date) -> dict:
    """Cash the re-match pass recovered on this day, as its own line:
    the allocations that entered through a PACK-REMATCH-<day> pack,
    grouped by bank line. A recovery is a proposal at the gate, like
    any fresh match — the pack's status says how far it got."""
    prefix = f"PACK-REMATCH-{day.isoformat()}"
    by_line: dict[str, Decimal] = {}
    for pack in store.packs():
        if pack.pack_id != prefix and not pack.pack_id.startswith(prefix + "-"):
            continue
        for entry in pack.entries:
            if entry.allocation:
                by_line[entry.allocation.bank_line_id] = (
                    by_line.get(entry.allocation.bank_line_id, ZERO) + entry.allocation.amount
                )
    return {
        "recovered_total": str(sum(by_line.values(), ZERO)),
        "recovered_lines": len(by_line),
        "items": [
            {"bank_line_id": k, "amount": str(v)} for k, v in sorted(by_line.items())
        ],
    }


def suspense_aging(store: Store, as_of: date) -> list[dict]:
    """All unapplied cash, bucketed by age, oldest named in each.

    Amounts are never added across currencies: every item names its
    own denomination, and each bucket carries its totals per
    currency beside the headline figure (which, on a
    single-currency book, is simply that currency's total)."""
    items = [i for i in store.suspense_items() if i.status == "open"]
    out = []
    for low, high, label in BUCKETS:
        members = [
            i for i in items
            if i.age_days(as_of) >= low and (high is None or i.age_days(as_of) <= high)
        ]
        members.sort(key=lambda i: i.received_date)
        by_currency: dict[str, Decimal] = {}
        for i in members:
            by_currency[i.currency] = by_currency.get(i.currency, ZERO) + i.amount
        out.append(
            {
                "bucket": label,
                "total": str(sum((i.amount for i in members), ZERO)),
                "totals_by_currency": {
                    ccy: str(total) for ccy, total in sorted(by_currency.items())
                },
                "count": len(members),
                "items": [
                    {
                        "suspense_id": i.suspense_id,
                        "bank_line_id": i.bank_line_id,
                        "amount": str(i.amount),
                        "currency": i.currency,
                        "received_date": i.received_date.isoformat(),
                        "age_days": i.age_days(as_of),
                        "reason": i.reason,
                    }
                    for i in members
                ],
            }
        )
    return out


def returns_view(store: Store, on: date | None = None) -> dict:
    """Payment returns as a control line: the count and total of the
    amounts the bank took back, with part-returns carried distinctly
    (their own count and total) — a return of part of a line is a
    different control fact from a line coming back whole. With
    `on`, only the returns recorded that day — the reversing
    entries belonging to that day's report. Without it, every
    return on the books."""
    rets = store.returns(on=on)
    totals_by_currency: dict[str, Decimal] = {}
    for r in rets:
        totals_by_currency[r.currency] = totals_by_currency.get(r.currency, ZERO) + r.amount
    partials = [r for r in rets if r.partial]
    return {
        "count": len(rets),
        "total": str(sum((r.amount for r in rets), ZERO)),
        "totals_by_currency": {
            ccy: str(total) for ccy, total in sorted(totals_by_currency.items())
        },
        "partial_count": len(partials),
        "partial_total": str(sum((r.amount for r in partials), ZERO)),
        "items": [
            {
                "return_id": r.return_id,
                "bank_line_id": r.bank_line_id,
                "reason": r.reason.value,
                "amount": str(r.amount),
                "currency": r.currency,
                "partial": r.partial,
                "return_date": r.return_date.isoformat(),
                "recorded_by": r.recorded_by,
                "pack_ids": list(r.pack_ids),
                "detail": r.detail,
            }
            for r in rets
        ],
    }


def _return_premium_day_lines(store: Store, day: date, netted_total: Decimal) -> dict:
    """The day's return-premium control lines: payables a
    cancellation opened today, the cash netted against payables in
    today's packs (computed by the caller from the pack entries),
    and the payout packs approved with today's value date. These
    describe the payable ledger; they are not terms of the bank
    identity beyond the netting, which the footing already names."""
    opened = [p for p in store.payables() if p.opened_on == day]
    payouts = [
        p for p in store.payout_packs(on=day) if p.status.value == "approved"
    ]
    return {
        "payables_opened": {
            "count": len(opened),
            "total": str(sum((p.amount for p in opened), ZERO)),
            "payable_ids": [p.payable_id for p in opened],
        },
        "netted_total": str(netted_total),
        "payouts_approved": {
            "count": len(payouts),
            "total": str(sum((p.total for p in payouts), ZERO)),
            "pack_ids": [p.pack_id for p in payouts],
        },
    }


def return_premiums_view(store: Store) -> dict:
    """Every return-premium payable on the books, with its chain:
    the cancellation that opened it, what incoming cash netted
    against it, what payout packs settled, and what still stands.
    The read model behind the console's payables table and
    /api/payables."""
    payables = store.payables()
    return {
        "open_count": len([p for p in payables if p.status == "open"]),
        "open_total": str(sum((p.remaining for p in payables if p.status == "open"), ZERO)),
        "netted_total": str(sum((p.amount_netted for p in payables), ZERO)),
        "paid_total": str(sum((p.amount_paid for p in payables), ZERO)),
        "payables": [
            {
                "payable_id": p.payable_id,
                "policy_number": p.policy_number,
                "cancellation_id": p.cancellation_id,
                "payee_kind": p.payee_kind,
                "payee_name": p.payee_name,
                "broker_id": p.broker_id,
                "amount": str(p.amount),
                "amount_netted": str(p.amount_netted),
                "amount_paid": str(p.amount_paid),
                "remaining": str(p.remaining),
                "status": p.status,
                "opened_on": p.opened_on.isoformat(),
                "events": [
                    {"on": e.on.isoformat(), "kind": e.kind, "amount": str(e.amount),
                     "ref": e.ref, "note": e.note}
                    for e in p.events
                ],
            }
            for p in payables
        ],
    }


def day_report(store: Store, day: date, policy: PolicyConfig) -> dict:
    lines = store.bank_lines(on=day)
    packs = store.packs(on=day)
    line_ids = {l.bank_line_id for l in lines}
    allocations = [a for a in store.allocations() if a.bank_line_id in {l.bank_line_id for l in lines}]
    suspense_today = [
        i for i in store.suspense_items() if i.received_date == day and i.status == "open"
    ]
    exceptions_today = [e for e in store.exceptions() if e.first_seen == day and e.code.value != "STALE_SUSPENSE"]

    bank_total = sum((l.amount for l in lines), ZERO)
    allocated_total = sum((a.amount for a in allocations), ZERO)
    # Cash netted against return-premium payables rides the day's
    # packs as offset entries: it is neither allocated to an
    # installment nor sitting in suspense, and the footing identity
    # names it as its own term rather than losing it.
    netted_by_line: dict[str, Decimal] = {}
    for pack in packs:
        for entry in pack.entries:
            if entry.payable_offset and entry.payable_offset.bank_line_id in line_ids:
                lid = entry.payable_offset.bank_line_id
                netted_by_line[lid] = netted_by_line.get(lid, ZERO) + entry.payable_offset.amount
    netted_total = sum(netted_by_line.values(), ZERO)
    suspense_total = sum((i.amount for i in suspense_today), ZERO)
    posted_total = sum(
        (
            entry.allocation.amount
            for pack in packs
            if pack.status.value == "approved"
            for entry in pack.entries
            if entry.allocation and entry.allocation.bank_line_id in line_ids
        ),
        ZERO,
    )

    # Per-broker view: a line belongs to the broker who sent it (or the
    # broker on the policy it references), else to the direct book.
    # Each broker's figures are also kept per currency — a broker's
    # EUR bank total is never added to its USD one.
    brokers = {b.broker_id: b.name for b in store.brokers()}
    by_broker: dict[str, dict] = {}
    alloc_by_line: dict[str, Decimal] = {}
    for a in allocations:
        alloc_by_line[a.bank_line_id] = alloc_by_line.get(a.bank_line_id, ZERO) + a.amount
    susp_by_line: dict[str, Decimal] = {}
    for i in suspense_today:
        susp_by_line[i.bank_line_id] = susp_by_line.get(i.bank_line_id, ZERO) + i.amount
    for line in lines:
        key = _broker_key(store, line, brokers)
        bucket = by_broker.setdefault(
            key, {"bank": ZERO, "allocated": ZERO, "netted": ZERO, "suspense": ZERO,
                  "currencies": {}}
        )
        bucket["bank"] += line.amount
        bucket["allocated"] += alloc_by_line.get(line.bank_line_id, ZERO)
        bucket["netted"] += netted_by_line.get(line.bank_line_id, ZERO)
        bucket["suspense"] += susp_by_line.get(line.bank_line_id, ZERO)
        cbucket = bucket["currencies"].setdefault(
            line.currency,
            {"bank": ZERO, "allocated": ZERO, "netted": ZERO, "suspense": ZERO})
        cbucket["bank"] += line.amount
        cbucket["allocated"] += alloc_by_line.get(line.bank_line_id, ZERO)
        cbucket["netted"] += netted_by_line.get(line.bank_line_id, ZERO)
        cbucket["suspense"] += susp_by_line.get(line.bank_line_id, ZERO)

    non_premium = non_premium_view(store, day)
    non_premium_total = Decimal(non_premium["total"])
    # The footing runs per currency. Each currency's bank total must
    # equal its own applied + netted + suspense + non-premium, in
    # that currency; the day foots when every currency present
    # foots. On a single-currency day this is exactly the identity
    # the report has always run.
    currencies: dict[str, dict] = {}
    for line in lines:
        currencies.setdefault(line.currency, {
            "bank": ZERO, "allocated": ZERO, "posted": ZERO,
            "netted": ZERO, "suspense": ZERO, "non_premium": ZERO,
        })
    line_ccy = {l.bank_line_id: l.currency for l in lines}
    for a in allocations:
        currencies[a.currency]["allocated"] += a.amount
    for pack in packs:
        if pack.status.value != "approved":
            continue
        for entry in pack.entries:
            if entry.allocation and entry.allocation.bank_line_id in line_ids:
                currencies[entry.allocation.currency]["posted"] += entry.allocation.amount
    for lid, amount in netted_by_line.items():
        currencies[line_ccy[lid]]["netted"] += amount
    for i in suspense_today:
        currencies[i.currency]["suspense"] += i.amount
    for item in non_premium["items"]:
        currencies.setdefault(item["currency"], {
            "bank": ZERO, "allocated": ZERO, "posted": ZERO,
            "netted": ZERO, "suspense": ZERO, "non_premium": ZERO,
        })
        currencies[item["currency"]]["non_premium"] += Decimal(item["amount"])
    for ccy, slot in currencies.items():
        slot["bank"] = sum(
            (l.amount for l in lines if l.currency == ccy), ZERO)
        slot["footing_ok"] = (
            slot["bank"]
            == slot["allocated"] + slot["netted"] + slot["suspense"] + slot["non_premium"]
        )
    footing_ok = all(slot["footing_ok"] for slot in currencies.values()) if currencies else (
        bank_total == allocated_total + netted_total + suspense_total + non_premium_total
    )
    # The day's functional translation: every figure converted at
    # the governed rate in force on its line's value date (the day
    # itself, for a day report). Allocations convert at the figures
    # recorded on them when they were created. A translation for
    # presentation — never the control, never a posting.
    functional = {
        "currency": FUNCTIONAL_CURRENCY,
        "bank_total": str(sum(
            (to_functional(l.amount, l.currency, l.value_date) for l in lines), ZERO)),
        "allocated_total": str(sum(
            (
                a.functional_amount if a.functional_amount is not None
                else a.amount if a.currency == FUNCTIONAL_CURRENCY
                else to_functional(a.amount, a.currency, day)
                for a in allocations
            ), ZERO)),
        "suspense_total": str(sum(
            (to_functional(i.amount, i.currency, i.received_date) for i in suspense_today),
            ZERO)),
        "rates_used": {
            ccy: str(rate_on(ccy, day))
            for ccy in sorted(currencies) if ccy != FUNCTIONAL_CURRENCY
        },
        "note": (
            f"Functional ({FUNCTIONAL_CURRENCY}) figures are translations at the "
            "governed rates in force on each line's value date — a reporting "
            "conversion, not postings. The day's control is the per-currency "
            "footing above."
        ),
    }
    stale = [
        i for i in store.suspense_items()
        if i.status == "open" and i.age_days(day) > policy.stale_suspense_days
    ]
    # Returns touch the day two ways, both shown, neither netted
    # silently: returns RECORDED today (reversing entries belonging
    # to this report), and returns of lines RECEIVED today, which
    # restate what today's allocations are worth now.
    returned_for_lines = sum(
        (r.amount for r in store.returns() if r.bank_line_id in line_ids), ZERO
    )
    if len(currencies) <= 1:
        footing_note = (
            (
                "Bank total equals applied"
                + (" plus cash netted against return-premium payables"
                   if netted_total > ZERO else "")
                + " plus suspense"
                + (" plus non-premium dispositions (cash that is not premium "
                   "and never enters suspense)" if non_premium_total > ZERO else "")
                + ", to the cent."
            )
            if footing_ok
            else f"FOOTING FAILURE: bank {bank_total} != applied {allocated_total} "
            f"+ netted {netted_total} + suspense {suspense_total} "
            f"+ non-premium {non_premium_total}. The day is NOT reconciled."
        )
    elif footing_ok:
        footing_note = (
            "Bank equals applied plus suspense in each currency ("
            + ", ".join(sorted(currencies))
            + "), to the cent: the footing runs per currency and no figure "
            f"crosses denominations. The functional ({FUNCTIONAL_CURRENCY}) "
            "totals are translations at the governed rates listed, not postings."
        )
    else:
        failures = "; ".join(
            f"{ccy}: bank {slot['bank']} != applied {slot['allocated']} "
            f"+ netted {slot['netted']} + suspense {slot['suspense']} "
            f"+ non-premium {slot['non_premium']}"
            for ccy, slot in sorted(currencies.items()) if not slot["footing_ok"]
        )
        footing_note = f"FOOTING FAILURE: {failures}. The day is NOT reconciled."
    return {
        "date": day.isoformat(),
        "bank_total": str(bank_total),
        "allocated_total": str(allocated_total),
        "returned_total": str(returned_for_lines),
        "allocated_net_of_returns": str(allocated_total - returned_for_lines),
        "posted_total": str(posted_total),
        "suspense_total": str(suspense_total),
        "returns_recorded": returns_view(store, on=day),
        "bank_lines": len(lines),
        "footing_ok": footing_ok,
        "reconciled": footing_ok,
        "footing_note": footing_note,
        "currencies": {
            ccy: {
                "bank": str(slot["bank"]),
                "allocated": str(slot["allocated"]),
                "posted": str(slot["posted"]),
                "netted": str(slot["netted"]),
                "suspense": str(slot["suspense"]),
                "non_premium": str(slot["non_premium"]),
                "footing_ok": slot["footing_ok"],
            }
            for ccy, slot in sorted(currencies.items())
        },
        "functional": functional,
        "netted_total": str(netted_total),
        "return_premiums": _return_premium_day_lines(store, day, netted_total),
        "exceptions": [
            {
                "exception_id": e.exception_id,
                "code": e.code.value,
                "bank_line_id": e.bank_line_id,
                "policy_number": e.policy_number,
                "amount": str(e.amount),
                "currency": e.currency,
                "detail": e.detail,
                "status": e.status,
            }
            for e in exceptions_today
        ],
        "by_broker": {
            k: {
                "bank": str(v["bank"]),
                "allocated": str(v["allocated"]),
                "netted": str(v["netted"]),
                "suspense": str(v["suspense"]),
                "currencies": {
                    ccy: {kk: str(vv) for kk, vv in cv.items()}
                    for ccy, cv in sorted(v["currencies"].items())
                },
            }
            for k, v in sorted(by_broker.items())
        },
        "rematch_recoveries": rematch_recoveries(store, day),
        "non_premium": non_premium,
        "combination_settlements": combination_settlements(store, day),
        "suspense_aging": suspense_aging(store, day),
        "stale_suspense": [
            {
                "suspense_id": i.suspense_id,
                "bank_line_id": i.bank_line_id,
                "amount": str(i.amount),
                "currency": i.currency,
                "age_days": i.age_days(day),
            }
            for i in sorted(stale, key=lambda i: i.received_date)
        ],
    }


def proposal_quality(store: Store, month: tuple[int, int] | None = None) -> dict:
    """How the model's proposals fared at the gate — the proof
    report for the proof item.

    Every decided pack is walked; each allocation entry that stands
    on a model proposal (see gate._proposal_kind) is graded by what
    the approver actually did, never by anything the model or the
    desk said about it:

    - ACCEPTED_AS_PROPOSED — approved with the entry untouched;
    - ACCEPTED_WITH_EDITS — approved after a governed edit; the
      value edited is, per entry, the amount change when the target
      stood, or the full final amount when the money was redirected
      to a different installment or policy;
    - REJECTED — its pack was rejected, whatever the entry said.

    Grouped by proposal kind (remittance reads vs lump-sum
    proposals), with totals. Computed fresh from the pack records;
    there are no scores for individual humans — the subject is the
    model's work, and an edit's author is on the entry for anyone
    who needs the audit trail, not ranked here. With `month`, only
    packs whose value date falls in it count — the form the
    month-end close pack carries."""
    by_kind: dict[str, dict] = {}
    details: list[dict] = []

    def slot(kind: str) -> dict:
        return by_kind.setdefault(kind, {
            "proposals": 0, "accepted_as_proposed": 0,
            "accepted_with_edits": 0, "rejected": 0, "value_edited": ZERO,
        })

    packs = sorted(store.packs(), key=lambda p: (p.value_date, p.pack_id))
    for pack in packs:
        if pack.status.value not in ("approved", "rejected"):
            continue
        if month is not None and (pack.value_date.year, pack.value_date.month) != month:
            continue
        for entry in pack.entries:
            if entry.kind != "allocation" or not entry.allocation or not entry.proposal_kind:
                continue
            alloc = entry.allocation
            if pack.status.value == "rejected":
                outcome = "REJECTED"
            elif entry.edits:
                outcome = "ACCEPTED_WITH_EDITS"
            else:
                outcome = "ACCEPTED_AS_PROPOSED"
            edited_value = ZERO
            original_amount = alloc.amount
            if entry.edits:
                first, last = entry.edits[0], entry.edits[-1]
                original_amount = first.before_amount
                if (first.before_policy, first.before_installment) != (
                    last.after_policy, last.after_installment
                ):
                    edited_value = last.after_amount
                else:
                    edited_value = abs(last.after_amount - first.before_amount)
            s = slot(entry.proposal_kind)
            s["proposals"] += 1
            s[outcome.lower()] += 1
            s["value_edited"] += edited_value
            details.append({
                "pack_id": pack.pack_id,
                "bank_line_id": alloc.bank_line_id,
                "policy_number": alloc.policy_number,
                "installment_id": alloc.installment_id,
                "kind": entry.proposal_kind,
                "proposed_by": entry.proposed_by,
                "outcome": outcome,
                "amount": str(alloc.amount),
                "original_amount": str(original_amount),
                "edited_value": str(edited_value),
            })

    def render(s: dict) -> dict:
        return {
            "proposals": s["proposals"],
            "accepted_as_proposed": s["accepted_as_proposed"],
            "accepted_with_edits": s["accepted_with_edits"],
            "rejected": s["rejected"],
            "value_edited": str(s["value_edited"]),
        }

    totals = {
        "proposals": sum(s["proposals"] for s in by_kind.values()),
        "accepted_as_proposed": sum(s["accepted_as_proposed"] for s in by_kind.values()),
        "accepted_with_edits": sum(s["accepted_with_edits"] for s in by_kind.values()),
        "rejected": sum(s["rejected"] for s in by_kind.values()),
        "value_edited": str(sum((s["value_edited"] for s in by_kind.values()), ZERO)),
    }
    return {
        "by_kind": {k: render(v) for k, v in sorted(by_kind.items())},
        "totals": totals,
        "proposals": details,
    }


def broker_scorecard(store: Store, as_of: date, month: tuple[int, int] | None = None) -> list[dict]:
    """Per-broker read model over everything on the books.

    For each broker: the money (bank / allocated / open suspense,
    attributed exactly as the daily reports attribute it — sum the
    days' by-broker splits and you get these numbers back), the
    remittances received, how cleanly their netting settles, what
    their netting mistakes cost, how long their cash sits unapplied,
    and the exceptions still open against them. Plus the broker
    query notes the pipeline drafted for their mismatches — view
    only; the system drafts notes, it never sends them, and it
    scores brokers' paperwork, never people.

    Clean-netting rate: of the broker's lines that settled at T2 or
    T4 (the netting tiers), the share that raised no exception at
    all. Average days unapplied: over every suspense item the
    broker's lines produced, resolved items count to their
    resolution date, open ones to `as_of`.

    With `month=(year, month)` the card is scoped to that calendar
    month — bank lines received in it, remittances received in it,
    and the exceptions those lines raised — which is the form the
    month-end close pack carries. Without it, the card is cumulative
    over everything on the books, as before.
    """
    brokers = store.brokers()
    names = {b.broker_id: b.name for b in brokers}
    lines = store.bank_lines()
    if month is not None:
        lines = [l for l in lines if (l.value_date.year, l.value_date.month) == month]
    month_line_ids = {l.bank_line_id for l in lines} if month is not None else None
    alloc_by_line: dict[str, list] = {}
    for a in store.allocations():
        alloc_by_line.setdefault(a.bank_line_id, []).append(a)
    open_susp_by_line: dict[str, Decimal] = {}
    for i in store.suspense_items():
        if i.status == "open":
            open_susp_by_line[i.bank_line_id] = open_susp_by_line.get(i.bank_line_id, ZERO) + i.amount
    exc_by_line: dict[str, list] = {}
    for e in store.exceptions():
        if e.bank_line_id:
            exc_by_line.setdefault(e.bank_line_id, []).append(e)
    notes = [e for e in store.audit_events() if e.action == "broker_note_drafted"]
    remittances = store.remittances()
    if month is not None:
        remittances = [
            r for r in remittances if (r.received_date.year, r.received_date.month) == month
        ]
    all_suspense = store.suspense_items()

    rows: list[dict] = []
    for broker in sorted(brokers, key=lambda b: b.name):
        bid = broker.broker_id
        mine = [l for l in lines if _broker_key(store, l, names) == bid]
        mine_ids = {l.bank_line_id for l in mine}
        bank = sum((l.amount for l in mine), ZERO)
        allocated = sum(
            (a.amount for lid in mine_ids for a in alloc_by_line.get(lid, [])), ZERO
        )
        suspense = sum((open_susp_by_line.get(lid, ZERO) for lid in mine_ids), ZERO)
        settled = [
            l for l in mine
            if any(a.tier in (Tier.T2_NETTING, Tier.T4_REMITTANCE)
                   for a in alloc_by_line.get(l.bank_line_id, []))
        ]
        clean = [l for l in settled if not exc_by_line.get(l.bank_line_id)]
        rate = (
            str((Decimal(len(clean)) / Decimal(len(settled))).quantize(Decimal("0.0001")))
            if settled else None
        )
        mismatches = [
            e for e in store.exceptions()
            if e.code == ExceptionCode.NETTING_MISMATCH and e.broker_id == bid
            and (month_line_ids is None or e.bank_line_id in month_line_ids)
        ]
        open_by_code: dict[str, dict] = {}
        for e in store.exceptions(status="open"):
            if e.broker_id != bid:
                continue
            if month_line_ids is not None and e.bank_line_id not in month_line_ids:
                continue
            slot = open_by_code.setdefault(e.code.value, {"count": 0, "amount": ZERO})
            slot["count"] += 1
            slot["amount"] += e.amount
        items = [i for i in all_suspense if i.bank_line_id in mine_ids]
        days = [((i.resolved_on or as_of) - i.received_date).days for i in items]
        avg_days = (
            str(cents(Decimal(sum(days)) / Decimal(len(days)))) if days else None
        )
        # The same money, kept per currency: a broker whose book is
        # billed in two currencies shows two sets of figures, never
        # a blended total dressed as one denomination.
        by_currency: dict[str, dict] = {}
        for l in mine:
            slot = by_currency.setdefault(l.currency, {
                "bank_total": ZERO, "allocated_total": ZERO, "suspense_total": ZERO})
            slot["bank_total"] += l.amount
            slot["allocated_total"] += sum(
                (a.amount for a in alloc_by_line.get(l.bank_line_id, [])), ZERO)
            slot["suspense_total"] += open_susp_by_line.get(l.bank_line_id, ZERO)
        rows.append({
            "broker_id": bid,
            "broker": names[bid],
            "remittances_received": len([r for r in remittances if r.broker_id == bid]),
            "bank_total": str(bank),
            "allocated_total": str(allocated),
            "suspense_total": str(suspense),
            "by_currency": {
                ccy: {kk: str(vv) for kk, vv in slot.items()}
                for ccy, slot in sorted(by_currency.items())
            },
            "settled_netting_lines": len(settled),
            "clean_netting_lines": len(clean),
            "clean_netting_rate": rate,
            "netting_mismatches": len(mismatches),
            "netting_mismatch_delta": str(sum((e.amount for e in mismatches), ZERO)),
            "avg_days_unapplied": avg_days,
            "suspense_items_counted": len(items),
            "open_exceptions_by_code": {
                k: {"count": v["count"], "amount": str(v["amount"])}
                for k, v in sorted(open_by_code.items())
            },
            "query_notes": [
                {"bank_line_id": e.subject, "at": e.at.isoformat(), "note": e.detail}
                for e in notes
                if e.subject in mine_ids
            ],
        })
    return rows


def _broker_key(store: Store, line, broker_names: dict[str, str]) -> str:
    for broker_id, name in broker_names.items():
        if name.lower() in line.payer_name.lower():
            return broker_id
    if line.reference:
        rec = store.receivable(line.reference)
        if rec and rec.broker_id:
            return rec.broker_id
    return "DIRECT/UNKNOWN"
