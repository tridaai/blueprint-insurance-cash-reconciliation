"""The daily reconciliation report — the control the day is judged by.

One question, answered to the cent: does the bank total equal what was
applied plus what sits in suspense? If the footing fails, the report
says so in words and refuses to call the day reconciled. Exceptions
are listed, never netted away; suspense is aged in buckets with the
oldest items named, because aged unapplied cash is what auditors ask
about first.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import Policy as PolicyConfig
from .models import ExceptionCode, Tier
from .money import ZERO, cents
from .store import Store

BUCKETS = [(0, 7, "0–7 days"), (8, 30, "8–30 days"), (31, 60, "31–60 days"), (61, None, "60+ days")]


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
    """All unapplied cash, bucketed by age, oldest named in each."""
    items = [i for i in store.suspense_items() if i.status == "open"]
    out = []
    for low, high, label in BUCKETS:
        members = [
            i for i in items
            if i.age_days(as_of) >= low and (high is None or i.age_days(as_of) <= high)
        ]
        members.sort(key=lambda i: i.received_date)
        out.append(
            {
                "bucket": label,
                "total": str(sum((i.amount for i in members), ZERO)),
                "count": len(members),
                "items": [
                    {
                        "suspense_id": i.suspense_id,
                        "bank_line_id": i.bank_line_id,
                        "amount": str(i.amount),
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
    full-line amounts the bank took back. With `on`, only the returns
    recorded that day — the reversing entries belonging to that day's
    report. Without it, every return on the books."""
    rets = store.returns(on=on)
    return {
        "count": len(rets),
        "total": str(sum((r.amount for r in rets), ZERO)),
        "items": [
            {
                "return_id": r.return_id,
                "bank_line_id": r.bank_line_id,
                "reason": r.reason.value,
                "amount": str(r.amount),
                "return_date": r.return_date.isoformat(),
                "recorded_by": r.recorded_by,
                "pack_ids": list(r.pack_ids),
                "detail": r.detail,
            }
            for r in rets
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
        bucket = by_broker.setdefault(key, {"bank": ZERO, "allocated": ZERO, "suspense": ZERO})
        bucket["bank"] += line.amount
        bucket["allocated"] += alloc_by_line.get(line.bank_line_id, ZERO)
        bucket["suspense"] += susp_by_line.get(line.bank_line_id, ZERO)

    footing_ok = bank_total == allocated_total + suspense_total
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
        "footing_note": (
            "Bank total equals applied plus suspense, to the cent."
            if footing_ok
            else f"FOOTING FAILURE: bank {bank_total} != applied {allocated_total} "
            f"+ suspense {suspense_total}. The day is NOT reconciled."
        ),
        "exceptions": [
            {
                "exception_id": e.exception_id,
                "code": e.code.value,
                "bank_line_id": e.bank_line_id,
                "policy_number": e.policy_number,
                "amount": str(e.amount),
                "detail": e.detail,
                "status": e.status,
            }
            for e in exceptions_today
        ],
        "by_broker": {
            k: {kk: str(vv) for kk, vv in v.items()} for k, v in sorted(by_broker.items())
        },
        "rematch_recoveries": rematch_recoveries(store, day),
        "suspense_aging": suspense_aging(store, day),
        "stale_suspense": [
            {
                "suspense_id": i.suspense_id,
                "bank_line_id": i.bank_line_id,
                "amount": str(i.amount),
                "age_days": i.age_days(day),
            }
            for i in sorted(stale, key=lambda i: i.received_date)
        ],
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
        rows.append({
            "broker_id": bid,
            "broker": names[bid],
            "remittances_received": len([r for r in remittances if r.broker_id == bid]),
            "bank_total": str(bank),
            "allocated_total": str(allocated),
            "suspense_total": str(suspense),
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
