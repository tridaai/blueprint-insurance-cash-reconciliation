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

The pack body contains no wall-clock stamps. Its SHA-256 digest is
computed over the canonical body, so the exported JSON and the
human-readable text rendering are two views of one record.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .money import ZERO, as_str
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
    line releases the whole remaining position on the return date —
    the cash went back to the payer, so it is in suspense no longer,
    and the roll names that as its own term (returned_from_suspense)
    rather than folding it into applications."""

    def __init__(self, item, created: Decimal | None, releases: list[tuple[date, Decimal]],
                 returned_on: date | None = None):
        self.item = item
        self.created = created  # None when the bank line is not on the books
        self.releases = sorted(releases)
        self.returned_on = returned_on

    def open_at(self, as_of: date) -> Decimal:
        if self.created is None:
            return self.item.amount
        if self.returned_on is not None and as_of >= self.returned_on:
            return ZERO
        released = sum((amt for d, amt in self.releases if d <= as_of), ZERO)
        return self.created - released

    def released_by_return(self) -> Decimal:
        """The amount the return itself released: what was still open
        on the return date. Zero when the line was never returned."""
        if self.returned_on is None or self.created is None:
            return ZERO
        released = sum((amt for d, amt in self.releases if d <= self.returned_on), ZERO)
        return self.created - released


def _positions(store: Store) -> tuple[list[_Position], dict[str, Decimal]]:
    lines = {l.bank_line_id: l for l in store.bank_lines()}
    returned_on = {r.bank_line_id: r.return_date for r in store.returns()}
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
            else:
                day_alloc[entry.allocation.bank_line_id] += entry.allocation.amount
    positions = []
    for item in store.suspense_items():
        line = lines.get(item.bank_line_id)
        created = None if line is None else line.amount - day_alloc.get(item.bank_line_id, ZERO)
        positions.append(_Position(item, created, releases.get(item.bank_line_id, []),
                                   returned_on.get(item.bank_line_id)))
    return positions, lines


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
        out.append({
            "bucket": label,
            "total": as_str(sum((amount for _p, amount in bucket), ZERO)),
            "count": len(bucket),
            "items": [
                {
                    "suspense_id": pos.item.suspense_id,
                    "bank_line_id": pos.item.bank_line_id,
                    "amount": as_str(amount),
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

    opening = sum(
        (pos.open_at(day_before) for pos in positions if pos.item.received_date < first),
        ZERO,
    )
    received = sum(
        (pos.created if pos.created is not None else pos.item.amount)
        for pos in positions if in_month(pos.item.received_date)
    ) if positions else ZERO
    applied = sum(
        (amt for pos in positions for d, amt in pos.releases if in_month(d)),
        ZERO,
    )
    returned_from_suspense = sum(
        (pos.released_by_return() for pos in positions
         if pos.returned_on is not None and in_month(pos.returned_on)),
        ZERO,
    )
    closing = sum(
        (pos.open_at(last) for pos in positions if pos.item.received_date <= last),
        ZERO,
    )
    roll_closing = opening + received - applied - returned_from_suspense
    difference = roll_closing - closing

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
        (l.amount for l in lines.values() if in_month(l.value_date)), ZERO
    )

    def exc_view(e):
        return {
            "exception_id": e.exception_id,
            "code": e.code.value,
            "bank_line_id": e.bank_line_id,
            "policy_number": e.policy_number,
            "broker_id": e.broker_id,
            "amount": as_str(e.amount),
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

    footed = difference == 0 and not discrepancies
    if footed:
        footing_note = (
            "The month FOOTS: opening suspense + cash received into suspense "
            "− applied from suspense"
            + (" − returned from suspense" if returned_from_suspense > ZERO else "")
            + " = closing suspense, to the cent, and the "
            "closing figure agrees with the suspense ledger."
        )
    else:
        bits = []
        if difference != 0:
            bits.append(f"the control roll misses the ledger by {as_str(difference)}")
        bits += [d["problem"] for d in discrepancies]
        footing_note = "NOT FOOTED: " + "; ".join(bits) + "."

    body = {
        "month": f"{year:04d}-{month:02d}",
        "period": {"first": first.isoformat(), "last": last.isoformat()},
        "control_roll": {
            "opening_suspense": as_str(opening),
            "opening_aging": _aging(
                [p for p in positions if p.item.received_date < first], day_before
            ),
            "cash_received_in_month": as_str(cash_received),
            "received_into_suspense": as_str(received),
            "applied_from_suspense": as_str(applied),
            "returned_from_suspense": as_str(returned_from_suspense),
            "returns_in_month": {
                "count": len(month_returns),
                "total": as_str(sum((r.amount for r in month_returns), ZERO)),
            },
            "applied_allocations_approved": as_str(applied_approved),
            "approved_write_offs": as_str(write_offs),
            "closing_suspense": as_str(closing),
            "roll_closing_suspense": as_str(roll_closing),
            "difference": as_str(difference),
            "footed": footed,
            "footing_note": footing_note,
        },
        "payment_returns_in_month": [
            {
                "return_id": r.return_id,
                "bank_line_id": r.bank_line_id,
                "reason": r.reason.value,
                "return_date": r.return_date.isoformat(),
                "amount": as_str(r.amount),
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
        "discrepancies": discrepancies,
    }
    return body


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
        f"  Payment returns in month:    {roll['returns_in_month']['count']:>12}  totalling {roll['returns_in_month']['total']} (full bank lines, reversing entries below)",
        f"  Applied via approved packs:  {roll['applied_allocations_approved']:>12}",
        f"  Approved write-offs:         {roll['approved_write_offs']:>12}  (receivable deltas; they do not release suspense cash)",
        f"  Closing suspense:            {roll['closing_suspense']:>12}",
        f"  {roll['footing_note']}",
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
    if body["payment_returns_in_month"]:
        out += ["", f"PAYMENT RETURNS IN THE MONTH ({len(body['payment_returns_in_month'])})"]
        for r in body["payment_returns_in_month"]:
            out.append(
                f"  {r['return_id']} · {r['bank_line_id']} · {r['reason']} · "
                f"{r['amount']:>10} · returned {r['return_date']} · recorded by "
                f"{r['recorded_by']} · reversing packs: {', '.join(r['pack_ids']) or '—'}"
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
