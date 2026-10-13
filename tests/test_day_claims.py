"""Day claims: the store-level record that makes "a day runs once"
true across processes.

Round 10's guard read the audit trail and then wrote to it —
read-then-write, so two processes starting the same day at the
same moment could both pass it. These tests pin the claim
semantics the run now stands on: a completed run's claim is the
day's standing record; a second starter meeting a live attempt
(claim standing, run lock held) is refused in the same governed
way as any repeat, naming the claiming attempt; an interrupted
attempt's claim is taken over only by the recovery path, after
the lock proves no live attempt holds the day; and a starter
that died between claiming the day and its start reaching the
trail is recovered from the claim's own evidence.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.llm import OfflineReader
from cash_recon.models import AuditEvent, DayClaim
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import DayRunError, ingest_dataset, run_day
from cash_recon.store import InMemoryStore

DAY = date(2026, 9, 2)
POLICY = PolicyConfig()
ASSEMBLER = "cash-team"


def seeded(store=None):
    store = store if store is not None else InMemoryStore()
    objects = InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    return store, objects


def run_events(store, day=DAY):
    return [
        e.action for e in store.audit_events()
        if e.subject == day.isoformat() and e.action.startswith("day_run_")
    ]


def test_a_completed_run_holds_the_claim_as_the_days_record():
    store, objects = seeded()
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["attempt"] == 1 and result["recovered"] is False
    claim = store.day_claim(DAY)
    assert claim is not None
    assert claim.attempt == 1 and claim.state == "completed"
    assert claim.actor == ASSEMBLER and claim.thread_id == "day-2026-09-02-a1"
    # The run is over: its lock is not held by anyone.
    assert store.try_day_lock(DAY) is True
    store.release_day_lock(DAY)


def test_a_second_starter_is_refused_while_the_first_attempt_runs():
    """The round-10 race, staged deterministically: another
    process's attempt has claimed the day, written its start to
    the trail, and still holds the run lock. A starter now must
    refuse — not recover, not run — naming the claiming attempt,
    and the books and the claim stay exactly as they were."""
    store, objects = seeded()
    store.audit(AuditEvent(
        at=datetime.now(timezone.utc), actor="other-process",
        action="day_run_started", subject=DAY.isoformat(),
        detail="attempt 1 on thread day-2026-09-02-a1"))
    assert store.claim_day(DayClaim(
        day=DAY, attempt=1, actor="other-process",
        claimed_at=datetime.now(timezone.utc), state="running",
        thread_id="day-2026-09-02-a1"))
    assert store.try_day_lock(DAY) is True  # the live attempt holds the run

    with pytest.raises(DayRunError, match="already claimed") as refused:
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert "attempt 1" in str(refused.value)
    assert "other-process" in str(refused.value)

    # Nothing ran: no pack, no second start on the trail, and the
    # claim still stands in the first attempt's name.
    assert store.pack("PACK-2026-09-02") is None
    assert run_events(store) == ["day_run_started", "day_run_refused"]
    claim = store.day_claim(DAY)
    assert claim.attempt == 1 and claim.actor == "other-process"
    assert claim.state == "running"

    # The attempt dies (its process is gone; the lock went with
    # it). Now the same trail is an interrupted attempt, and the
    # sanctioned recovery — and only it — takes the claim over.
    store.release_day_lock(DAY)
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True and result["attempt"] == 2
    claim = store.day_claim(DAY)
    assert claim.attempt == 2 and claim.state == "completed"
    assert run_events(store) == [
        "day_run_started", "day_run_refused", "day_run_interrupted",
        "day_run_started", "day_run_completed",
    ]


def test_a_claim_without_a_start_recovers_from_the_claims_own_evidence():
    """A starter that claimed the day and died before its start
    reached the trail leaves no trail to read. The claim ahead of
    the trail's started count is itself the interruption proof,
    and the recovery takes it over the same way."""
    store, objects = seeded()
    assert store.claim_day(DayClaim(
        day=DAY, attempt=1, actor="gone-process",
        claimed_at=datetime.now(timezone.utc), state="running",
        thread_id="day-2026-09-02-a1"))

    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True and result["attempt"] == 1
    assert run_events(store) == [
        "day_run_interrupted", "day_run_started", "day_run_completed"]
    interrupted = next(
        e for e in store.audit_events() if e.action == "day_run_interrupted")
    assert "attempt 1" in interrupted.detail and "never started" in interrupted.detail
    claim = store.day_claim(DAY)
    assert claim.attempt == 1 and claim.state == "completed"


class CrashMidRun(InMemoryStore):
    """Dies on the second exception write of the run, as in the
    round-10 lifecycle tests: the run is half-persisted."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True
        self.calls = 0

    def add_exception(self, exc):
        if self.armed:
            self.calls += 1
            if self.calls == 2:
                self.armed = False
                raise RuntimeError("simulated crash mid-run")
        super().add_exception(exc)


def test_a_crash_leaves_the_claim_running_and_the_lock_free():
    store, objects = seeded(CrashMidRun())
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)

    # The interrupted attempt's claim stands, still running — the
    # day is neither lost nor doubly owned.
    claim = store.day_claim(DAY)
    assert claim.attempt == 1 and claim.state == "running"
    # And its lock died with it: a lock the dead attempt still
    # held would block the recovery forever.
    assert store.try_day_lock(DAY) is True
    store.release_day_lock(DAY)

    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True and result["attempt"] == 2
    claim = store.day_claim(DAY)
    assert claim.attempt == 2 and claim.state == "completed"
