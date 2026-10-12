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
from .money import ZERO
from .store import Store

BUCKETS = [(0, 7, "0–7 days"), (8, 30, "8–30 days"), (31, 60, "31–60 days"), (61, None, "60+ days")]


def suspense_aging(store: Store, as_of: date) -> list[dict]:
    """All unapplied cash, bucketed by age, oldest named in each."""
    items = store.suspense_items()
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


def day_report(store: Store, day: date, policy: PolicyConfig) -> dict:
    lines = store.bank_lines(on=day)
    packs = store.packs(on=day)
    line_ids = {l.bank_line_id for l in lines}
    allocations = [a for a in store.allocations() if a.bank_line_id in {l.bank_line_id for l in lines}]
    suspense_today = [i for i in store.suspense_items() if i.received_date == day]
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
        i for i in store.suspense_items() if i.age_days(day) > policy.stale_suspense_days
    ]
    return {
        "date": day.isoformat(),
        "bank_total": str(bank_total),
        "allocated_total": str(allocated_total),
        "posted_total": str(posted_total),
        "suspense_total": str(suspense_total),
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


def _broker_key(store: Store, line, broker_names: dict[str, str]) -> str:
    for broker_id, name in broker_names.items():
        if name.lower() in line.payer_name.lower():
            return broker_id
    if line.reference:
        rec = store.receivable(line.reference)
        if rec and rec.broker_id:
            return rec.broker_id
    return "DIRECT/UNKNOWN"
