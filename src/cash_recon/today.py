"""The Today view: the desk's morning, in order, as a checklist.

Six questions open every day in this operation, and until now
answering them meant walking five tabs. The Today view walks them
in the order the desk works them — statement in? day run done?
what is still exceptioned? what waits at the gate? has suspense
been re-matched against the books as they now stand? were any
cancellations recorded? — and answers each from the same read
models the tabs use. It holds no state of its own: every figure is
recomputed from the books on each read, a step is "done" exactly
when the underlying act has happened, and each step names the tab
where the work is done. Returns recorded today ride on the
cancellations step: both are the day's movements on policies and
payables, and the step's detail counts them separately.
"""

from __future__ import annotations

from datetime import date

from .config import Policy as PolicyConfig
from .money import ZERO
from .store import Store


def _step(key: str, label: str, state: str, detail: str, tab: str) -> dict:
    return {"key": key, "label": label, "state": state, "detail": detail, "tab": tab}


def today_view(store: Store, day: date, policy: PolicyConfig) -> dict:
    """The day-open checklist for `day`, computed from the books."""
    lines = store.bank_lines(on=day)
    bank_total = sum((l.amount for l in lines), ZERO)

    steps: list[dict] = []

    # 1 — the statement: bank lines for the day are on the books.
    if lines:
        steps.append(_step(
            "statement", "Statement imported", "done",
            f"{len(lines)} bank line(s) totalling {bank_total} are on the books for {day}.",
            "import"))
    else:
        steps.append(_step(
            "statement", "Statement imported", "pending",
            f"No bank lines for {day} yet — import the day's statement first.",
            "import"))

    # 2 — the day run: the day's pack exists.
    day_pack = store.pack(f"PACK-{day.isoformat()}")
    if day_pack is not None:
        steps.append(_step(
            "day_run", "Day run complete", "done",
            f"Pack {day_pack.pack_id} was assembled and is {day_pack.status.value}.",
            "report"))
    else:
        steps.append(_step(
            "day_run", "Day run complete", "pending",
            "No pack for the day yet — run the day once the statement is in.",
            "report"))

    # 3 — exceptions: the open queue, with its oldest item.
    open_exceptions = store.exceptions(status="open")
    if not open_exceptions:
        steps.append(_step(
            "exceptions", "Exceptions open", "done",
            "Nothing is exceptioned and open — the queue is clear.",
            "exceptions"))
    else:
        oldest = min(open_exceptions, key=lambda e: (e.first_seen, e.exception_id))
        total = sum((e.amount for e in open_exceptions), ZERO)
        steps.append(_step(
            "exceptions", "Exceptions open", "pending",
            f"{len(open_exceptions)} open, {total} at issue; the oldest is "
            f"{oldest.exception_id} ({oldest.code.value}, {oldest.amount}), first seen "
            f"{oldest.first_seen} — {max((day - oldest.first_seen).days, 0)} day(s) old.",
            "exceptions"))

    # 4 — the gate: nothing proposed anywhere is waiting.
    proposed = [p for p in store.packs() if p.status.value == "proposed"]
    proposed_payouts = [p for p in store.payout_packs() if p.status.value == "proposed"]
    waiting = [p.pack_id for p in proposed] + [p.pack_id for p in proposed_payouts]
    if not waiting:
        steps.append(_step(
            "gate", "Packs awaiting the gate", "done",
            "No pack is waiting — everything assembled has been decided.",
            "packs"))
    else:
        steps.append(_step(
            "gate", "Packs awaiting the gate", "pending",
            f"{len(waiting)} pack(s) wait for a named decision: {', '.join(waiting)}.",
            "packs"))

    # 5 — the re-match: the pass stamps its attempt records with
    # the business date it ran for, so attempts dated today are on
    # the audit trail — or a re-match pack carries the day's date.
    attempts = [
        e for e in store.audit_events()
        if e.action == "suspense_rematch_attempt" and e.at.date() == day
    ]
    rematch_pack = store.pack(f"PACK-REMATCH-{day.isoformat()}")
    if attempts or rematch_pack is not None:
        bits = []
        if attempts:
            bits.append(f"{len(attempts)} item(s) re-matched today")
        if rematch_pack is not None:
            bits.append(f"recovery pack {rematch_pack.pack_id} is {rematch_pack.status.value}")
        steps.append(_step(
            "rematch", "Suspense re-match tried", "done",
            "; ".join(bits) + ".", "suspense"))
    else:
        open_suspense = [i for i in store.suspense_items() if i.status == "open"]
        steps.append(_step(
            "rematch", "Suspense re-match tried", "pending",
            f"Not tried today; {len(open_suspense)} suspense item(s) stand open against "
            "the books as they now stand.",
            "suspense"))

    # 6 — the day's policy movements: cancellations recorded today
    # (and the payables they opened), returns recorded today.
    cancellations = [c for c in store.cancellations() if c.recorded_on == day]
    returns = [r for r in store.returns(on=day)]
    new_payables = sum((c.return_premium for c in cancellations), ZERO)
    detail = (
        f"{len(cancellations)} cancellation(s) recorded today"
        + (f", opening {new_payables} of return-premium payables" if cancellations else "")
        + f"; {len(returns)} bank return(s) recorded today."
    )
    steps.append(_step(
        "returns", "Cancellations & returns recorded", "info", detail, "packs"))

    pending = sum(1 for s in steps if s["state"] == "pending")
    return {
        "day": day.isoformat(),
        "steps": steps,
        "pending": pending,
        "ready": pending == 0,
    }
