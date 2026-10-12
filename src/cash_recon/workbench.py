"""The exception workbench: how the desk works the queue daily.

The exception queue is where unapplied money waits for a human.
The workbench gives that queue an owner and a memory:

- Every open exception can be ASSIGNED to a named member of the
  cash team (the roster in dataset.TEAM_ROSTER — assignment to a
  name off the roster is refused, in words).
- Every exception carries dated HANDLING NOTES, append-only and
  attributed. There is deliberately no edit or delete path: what
  the desk knew, and when it knew it, is part of the record.

Assignment and notes are handling metadata. They never change an
exception's amount, code, or disposition — dispositions happen only
through the governed paths that already exist (a gate pack, the
re-match pass, a recorded return), and the workbench links to those
paths rather than growing disposition buttons of its own.

The view itself is a read model, computed fresh: the open queue
oldest first, filterable by code, broker, assignee, or unassigned,
with counts by code across the whole queue that foot to the queue
totals — the desk lead's morning question is "how much is waiting,
of what, and whose is it", and the counts answer it without a
second, disagreeing number anywhere.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .dataset import TEAM_ROSTER
from .models import AuditEvent, HandlingNote
from .money import ZERO, as_str
from .store import Store


class WorkbenchError(RuntimeError):
    """A workbench act was refused. The message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _find(store: Store, exception_id: str):
    exc = next((e for e in store.exceptions() if e.exception_id == exception_id), None)
    if exc is None:
        raise WorkbenchError(f"no such exception: {exception_id}")
    return exc


def assign_exception(store: Store, exception_id: str, assignee: str, actor: str):
    """Assign an open exception to a rostered team member. The act
    is audited; the exception's amount, code and status are not
    touched."""
    if not actor or not actor.strip():
        raise WorkbenchError("assignment is made by a named person")
    if assignee not in TEAM_ROSTER:
        raise WorkbenchError(
            f"{assignee!r} is not on the cash team roster ({', '.join(TEAM_ROSTER)}); "
            "an exception is assigned to a rostered member or left unassigned"
        )
    exc = _find(store, exception_id)
    if exc.status != "open":
        raise WorkbenchError(
            f"exception {exception_id} is {exc.status}; the workbench assigns open work"
        )
    previous = exc.assignee
    exc.assignee = assignee
    store.update_exception(exc)
    store.audit(AuditEvent(
        at=_now(), actor=actor, action="exception_assigned", subject=exception_id,
        detail=f"Assigned to {assignee}" + (f" (was {previous})" if previous else "") + "."))
    return exc


def add_handling_note(store: Store, exception_id: str, actor: str, text: str):
    """Append a dated, attributed handling note. Append-only by
    construction: this module exposes no way to edit or remove one."""
    if not actor or not actor.strip():
        raise WorkbenchError("a handling note is written by a named person")
    if not text or not text.strip():
        raise WorkbenchError("a handling note cannot be empty")
    exc = _find(store, exception_id)
    exc.handling_notes = [*exc.handling_notes, HandlingNote(at=_now(), by=actor, text=text.strip())]
    store.update_exception(exc)
    store.audit(AuditEvent(
        at=_now(), actor=actor, action="exception_note_added", subject=exception_id,
        detail=text.strip()))
    return exc


def workbench_view(store: Store, as_of: date, code: str | None = None,
                   broker_id: str | None = None, assignee: str | None = None,
                   unassigned: bool = False) -> dict:
    """The open queue, oldest first, with the counts that foot.

    counts_by_code and the totals describe the WHOLE open queue,
    unfiltered — they are the control the filtered rows are a view
    into, so they foot to the queue totals by construction."""
    queue = [e for e in store.exceptions(status="open")]
    queue.sort(key=lambda e: (e.first_seen, e.exception_id))

    counts: dict[str, dict] = {}
    for e in queue:
        slot = counts.setdefault(e.code.value, {"count": 0, "amount": ZERO})
        slot["count"] += 1
        slot["amount"] += e.amount

    def keep(e) -> bool:
        if code and e.code.value != code:
            return False
        if broker_id and e.broker_id != broker_id:
            return False
        if unassigned and e.assignee is not None:
            return False
        if assignee and e.assignee != assignee:
            return False
        return True

    rows = [
        {
            "exception_id": e.exception_id,
            "code": e.code.value,
            "bank_line_id": e.bank_line_id,
            "policy_number": e.policy_number,
            "broker_id": e.broker_id,
            "amount": as_str(e.amount),
            "detail": e.detail,
            "first_seen": e.first_seen.isoformat(),
            "age_days": (as_of - e.first_seen).days,
            "assignee": e.assignee,
            "notes": [
                {"at": n.at.isoformat(), "by": n.by, "text": n.text}
                for n in e.handling_notes
            ],
        }
        for e in queue if keep(e)
    ]
    return {
        "as_of": as_of.isoformat(),
        "roster": list(TEAM_ROSTER),
        "total_open": len(queue),
        "total_amount": as_str(sum((e.amount for e in queue), ZERO)),
        "counts_by_code": {
            k: {"count": v["count"], "amount": as_str(v["amount"])}
            for k, v in sorted(counts.items())
        },
        "filters": {
            "code": code, "broker_id": broker_id,
            "assignee": assignee, "unassigned": unassigned,
        },
        "filtered_count": len(rows),
        "rows": rows,
    }
