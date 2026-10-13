"""The match-quality trend watch: is the desk's matching getting
better or worse, month over month — answered from the books.

A read model over the same records the daily reports and close
packs summarise, so its money foots to theirs by construction:
the month's bank value is the sum of its days' bank totals, its
allocated value the sum of its days' allocated totals, and its
suspense received is the close pack's own reconstruction of what
entered suspense. Per month:

- **auto-applied rate** — the share of the month's bank value
  settled on entries the gate marked auto-appliable (exact T1
  matches at or below the policy threshold): cash that needed no
  human touch beyond the gate itself;
- **tier mix by value** — where the month's allocated money was
  settled, tier by tier (proposal allocations stand as PROPOSAL);
- **exception mix** — by code, count and value, value as a share
  of the month's bank value;
- **suspense dwell** — for suspense items received in the month
  and since resolved, the average days from receipt to
  resolution (re-match and returns both resolve items; an item
  still open has not finished dwelling and is not averaged in);
- **re-match recovery rate** — of the suspense items a re-match
  pass attempted in the month, the share it recovered into an
  approved re-match pack;
- **return rate** — returns recorded in the month, by count and
  by value as a share of the month's bank value.

Month-over-month deltas are computed here, in code. The watch
then names deteriorations only, against the tolerances in
`config.WatchPolicy`: an auto-rate drop beyond the line, an
exception code's value share doubling, dwell rising beyond the
line. When nothing crosses, the watch says **within tolerance**
— explicitly, with the figures — rather than staying silent.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .close_pack import _positions
from .config import WatchPolicy
from .money import ZERO, as_str, cents
from .store import Store

FOUR_PLACES = Decimal("0.0001")


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _of(d) -> str:
    return _month_key(d.year, d.month)


def _ratio(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole == ZERO:
        return None
    return (part / whole).quantize(FOUR_PLACES)


def _rstr(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _signed(value: Decimal | None, places: Decimal = FOUR_PLACES) -> str | None:
    if value is None:
        return None
    q = value.quantize(places)
    return f"+{q}" if q > ZERO else str(q)


def match_quality(store: Store, watch_policy: WatchPolicy | None = None) -> dict:
    """The trend over every month on the books, plus the watch
    verdict on the latest two. Pure read."""
    watch_policy = watch_policy or WatchPolicy()
    lines = store.bank_lines()
    lines_by_month: dict[str, list] = defaultdict(list)
    for line in lines:
        lines_by_month[_of(line.value_date)].append(line)
    month_of_line = {l.bank_line_id: _of(l.value_date) for l in lines}

    alloc_by_line: dict[str, list] = defaultdict(list)
    for a in store.allocations():
        alloc_by_line[a.bank_line_id].append(a)

    # Auto-appliable value is a property of pack entries: the gate
    # marked the entry when the pack was assembled.
    auto_by_month: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for pack in store.packs():
        for entry in pack.entries:
            if entry.allocation and entry.auto_applied:
                month = month_of_line.get(entry.allocation.bank_line_id)
                if month:
                    auto_by_month[month] += entry.allocation.amount

    exc_by_month: dict[str, dict[str, dict]] = defaultdict(dict)
    exc_value_by_month_ccy: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: ZERO))
    for e in store.exceptions():
        slot = exc_by_month[_of(e.first_seen)].setdefault(
            e.code.value, {"count": 0, "value": ZERO}
        )
        slot["count"] += 1
        slot["value"] += e.amount
        exc_value_by_month_ccy[_of(e.first_seen)][e.currency] += e.amount

    # Suspense received uses the close pack's own reconstruction
    # (what entered suspense when each line was worked), so this
    # figure is the close pack's received_into_suspense, by month.
    positions, _lines = _positions(store)
    received_by_month: dict[str, Decimal] = defaultdict(lambda: ZERO)
    dwell_by_month: dict[str, list[int]] = defaultdict(list)
    items_by_id = {}
    for pos in positions:
        item = pos.item
        items_by_id[item.suspense_id] = item
        created = pos.created if pos.created is not None else item.amount
        received_by_month[_of(item.received_date)] += created
        if item.resolved_on is not None:
            dwell_by_month[_of(item.received_date)].append(
                (item.resolved_on - item.received_date).days
            )

    # Re-match attempts are audited per item; a recovery is an
    # attempted item that now stands resolved with its cash in an
    # approved re-match pack.
    rematch_alloc_lines = {
        entry.allocation.bank_line_id
        for pack in store.packs()
        if pack.pack_id.startswith("PACK-REMATCH") and pack.status.value == "approved"
        for entry in pack.entries if entry.allocation
    }
    attempted_by_month: dict[str, set] = defaultdict(set)
    for e in store.audit_events():
        if e.action == "suspense_rematch_attempt":
            attempted_by_month[_of(e.at.date())].add(e.subject)

    returns_by_month: dict[str, list] = defaultdict(list)
    for r in store.returns():
        returns_by_month[_of(r.return_date)].append(r)

    months: list[dict] = []
    all_months = sorted(
        set(lines_by_month) | set(received_by_month) | set(returns_by_month)
        | set(attempted_by_month) | set(exc_by_month)
    )
    for key in all_months:
        month_lines = lines_by_month.get(key, [])
        bank_value = sum((l.amount for l in month_lines), ZERO)
        allocated = sum(
            (a.amount for l in month_lines for a in alloc_by_line.get(l.bank_line_id, [])),
            ZERO,
        )
        tier_mix: dict[str, dict] = {}
        for l in month_lines:
            for a in alloc_by_line.get(l.bank_line_id, []):
                tier = a.tier.value if a.tier else "PROPOSAL"
                slot = tier_mix.setdefault(tier, {"value": ZERO})
                slot["value"] += a.amount
        exc_mix = {}
        for code, slot in sorted(exc_by_month.get(key, {}).items()):
            exc_mix[code] = {
                "count": slot["count"],
                "value": as_str(slot["value"]),
                "share": _rstr(_ratio(slot["value"], bank_value)),
            }
        dwells = dwell_by_month.get(key, [])
        avg_dwell = (
            cents(Decimal(sum(dwells)) / Decimal(len(dwells))) if dwells else None
        )
        attempted = attempted_by_month.get(key, set())
        recovered = sum(
            1 for sid in attempted
            if (item := items_by_id.get(sid)) is not None
            and item.status == "resolved"
            and item.bank_line_id in rematch_alloc_lines
        )
        month_returns = returns_by_month.get(key, [])
        returns_value = sum((r.amount for r in month_returns), ZERO)
        auto_value = auto_by_month.get(key, ZERO)
        # The month's money, kept per currency: bank and allocated
        # value, the auto-applied rate within each currency, and
        # exception value. Rates and totals never blend
        # denominations — a rate computed over mixed currencies
        # would be a number about nothing.
        bank_by_ccy: dict[str, Decimal] = defaultdict(lambda: ZERO)
        alloc_by_ccy: dict[str, Decimal] = defaultdict(lambda: ZERO)
        auto_by_ccy: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for l in month_lines:
            bank_by_ccy[l.currency] += l.amount
            for a in alloc_by_line.get(l.bank_line_id, []):
                alloc_by_ccy[a.currency] += a.amount
        for pack in store.packs():
            for entry in pack.entries:
                if entry.allocation and entry.auto_applied:
                    if month_of_line.get(entry.allocation.bank_line_id) == key:
                        auto_by_ccy[entry.allocation.currency] += entry.allocation.amount
        months.append({
            "month": key,
            "bank_lines": len(month_lines),
            "bank_value": as_str(bank_value),
            "allocated_value": as_str(allocated),
            "bank_value_by_currency": {
                ccy: as_str(v) for ccy, v in sorted(bank_by_ccy.items())},
            "allocated_value_by_currency": {
                ccy: as_str(v) for ccy, v in sorted(alloc_by_ccy.items())},
            "auto_applied_rate_by_currency": {
                ccy: _rstr(_ratio(auto_by_ccy.get(ccy, ZERO), bank_by_ccy[ccy]))
                for ccy in sorted(bank_by_ccy)},
            "exception_value_by_currency": {
                ccy: as_str(v)
                for ccy, v in sorted(exc_value_by_month_ccy.get(key, {}).items())},
            "auto_applied_value": as_str(auto_value),
            "auto_applied_rate": _rstr(_ratio(auto_value, bank_value)),
            "tier_mix": {
                tier: {"value": as_str(slot["value"]),
                       "share": _rstr(_ratio(slot["value"], allocated))}
                for tier, slot in sorted(tier_mix.items())
            },
            "exception_mix": exc_mix,
            "exception_total_value": as_str(
                sum((Decimal(v["value"]) for v in exc_mix.values()), ZERO)
            ),
            "suspense_received_value": as_str(received_by_month.get(key, ZERO)),
            "suspense_resolved_items": len(dwells),
            "avg_suspense_dwell_days": as_str(avg_dwell) if avg_dwell is not None else None,
            "rematch": {
                "attempted": len(attempted),
                "recovered": recovered,
                "recovery_rate": _rstr(
                    _ratio(Decimal(recovered), Decimal(len(attempted)))
                ) if attempted else None,
            },
            "returns": {
                "count": len(month_returns),
                "value": as_str(returns_value),
                "rate": _rstr(_ratio(returns_value, bank_value)),
            },
            "deltas": {},
        })

    for prev, cur in zip(months, months[1:]):
        deltas = {}
        for field, places in (
            ("auto_applied_rate", FOUR_PLACES),
            ("avg_suspense_dwell_days", Decimal("0.01")),
            ("returns.rate", FOUR_PLACES),
        ):
            def pick(row, field=field):
                node = row
                for part in field.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                return Decimal(node) if node is not None else None

            p, c = pick(prev), pick(cur)
            label = field.replace(".", "_")
            deltas[label] = (
                _signed(c - p, places) if p is not None and c is not None else None
            )
        cur["deltas"] = deltas

    return {"months": months, "watch": evaluate_watch(months, watch_policy)}


def evaluate_watch(months: list[dict], policy: WatchPolicy) -> dict:
    """Name deteriorations between the last two months on the
    books — or say, explicitly, that the movement is within
    tolerance. Pure function over the trend rows."""
    if len(months) < 2:
        return {
            "status": "within tolerance",
            "compared": None,
            "findings": [],
            "note": "Only one month on the books: nothing to compare, "
                    "and no deterioration to name.",
        }
    prev, cur = months[-2], months[-1]
    findings: list[dict] = []

    def rate(row, field):
        node = row
        for part in field.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return Decimal(node) if node is not None else None

    prev_auto, cur_auto = rate(prev, "auto_applied_rate"), rate(cur, "auto_applied_rate")
    if prev_auto is not None and cur_auto is not None:
        drop = prev_auto - cur_auto
        if drop > policy.auto_rate_drop_points:
            findings.append({
                "kind": "AUTO_RATE_DROP",
                "detail": (
                    f"The auto-applied rate fell {drop} — from {prev_auto} in "
                    f"{prev['month']} to {cur_auto} in {cur['month']} — beyond "
                    f"the {policy.auto_rate_drop_points}-point tolerance. More "
                    "of the month's bank value needed a human touch beyond "
                    "the gate."
                ),
                "figures": {"previous": str(prev_auto), "current": str(cur_auto),
                            "drop": str(drop)},
            })

    codes = set(prev["exception_mix"]) | set(cur["exception_mix"])
    for code in sorted(codes):
        p = prev["exception_mix"].get(code, {}).get("share")
        c = cur["exception_mix"].get(code, {}).get("share")
        if p is None or c is None:
            continue
        p, c = Decimal(p), Decimal(c)
        if p > ZERO and c >= p * policy.exception_share_doubling_factor \
                and c >= policy.exception_share_floor:
            findings.append({
                "kind": "EXCEPTION_SHARE_DOUBLED",
                "code": code,
                "detail": (
                    f"{code}'s share of bank value rose from {p} in "
                    f"{prev['month']} to {c} in {cur['month']} — at least "
                    f"double, and past the {policy.exception_share_floor} "
                    "floor. The code's value on the month is "
                    f"{cur['exception_mix'][code]['value']} across "
                    f"{cur['exception_mix'][code]['count']} exception(s)."
                ),
                "figures": {"previous_share": str(p), "current_share": str(c),
                            "current_value": cur["exception_mix"][code]["value"]},
            })

    prev_dwell = rate(prev, "avg_suspense_dwell_days")
    cur_dwell = rate(cur, "avg_suspense_dwell_days")
    if prev_dwell is not None and cur_dwell is not None:
        rise = cur_dwell - prev_dwell
        if rise > policy.suspense_dwell_rise_days:
            findings.append({
                "kind": "SUSPENSE_DWELL_UP",
                "detail": (
                    f"Average suspense dwell rose {rise} days — from "
                    f"{prev_dwell} in {prev['month']} to {cur_dwell} in "
                    f"{cur['month']} — beyond the "
                    f"{policy.suspense_dwell_rise_days}-day tolerance. "
                    "Resolved cash is waiting longer before it is worked."
                ),
                "figures": {"previous": str(prev_dwell), "current": str(cur_dwell),
                            "rise": str(rise)},
            })

    if findings:
        note = (
            f"{len(findings)} named deterioration(s) between {prev['month']} "
            f"and {cur['month']}; each is stated with its figures above."
        )
        status = "findings"
    else:
        auto_move = (
            _signed(cur_auto - prev_auto) if prev_auto is not None and cur_auto is not None
            else "n/a"
        )
        dwell_move = (
            _signed(cur_dwell - prev_dwell, Decimal("0.01"))
            if prev_dwell is not None and cur_dwell is not None else "n/a"
        )
        note = (
            f"Within tolerance between {prev['month']} and {cur['month']}: "
            f"the auto-applied rate moved {auto_move} (a drop beyond "
            f"{policy.auto_rate_drop_points} points would be named), no "
            "exception code's share of bank value doubled, and average "
            f"suspense dwell moved {dwell_move} days (a rise beyond "
            f"{policy.suspense_dwell_rise_days} would be named)."
        )
        status = "within tolerance"
    return {
        "status": status,
        "compared": [prev["month"], cur["month"]],
        "findings": findings,
        "note": note,
    }
