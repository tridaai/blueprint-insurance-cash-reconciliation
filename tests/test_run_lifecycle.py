"""Safe re-runs and run lifecycle: a day runs once, an interrupted
run recovers, and the books after a refusal or a recovery are the
books a single clean run would have written.

The double-run facts these tests pin were established empirically
against the pre-guard code: a second run overwrote the day's pack
in place (an approved pack reverted to proposed on the record; a
rejection was erased), regenerated the day's exceptions over the
workbench's handling state, doubled the in-memory allocations
index, and left a day report that no longer footed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from cash_recon import pipeline
from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.gate import approve_pack, reject_pack
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import DayRunError, ingest_dataset, run_day
from cash_recon.store import InMemoryStore, dump
from cash_recon.workbench import add_handling_note, assign_exception

DAY = date(2026, 9, 2)
POLICY = PolicyConfig()
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def seeded(store=None):
    store = store if store is not None else InMemoryStore()
    objects = InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    return store, objects


def books(store) -> dict:
    """The books, canonically: every record family as sorted dumps,
    so two worlds can be compared exactly. The audit trail is not
    part of it — it is history, and history legitimately differs
    (a refusal and a recovery are events in it, by design)."""
    def digest(rows) -> str:
        return hashlib.sha256(b"|".join(dump(r).encode() for r in rows)).hexdigest()

    def pack_digest(pack) -> str:
        # decided_at is the one wall-clock field on a pack; two
        # worlds that decide seconds apart are the same books.
        row = json.loads(dump(pack))
        row.pop("decided_at", None)
        return json.dumps(row, sort_keys=True)

    return {
        "packs": hashlib.sha256(b"|".join(
            pack_digest(p).encode()
            for p in sorted(store.packs(), key=lambda p: p.pack_id))).hexdigest(),
        "pack_statuses": {p.pack_id: p.status.value for p in store.packs()},
        "exceptions": digest(sorted(store.exceptions(), key=lambda e: e.exception_id)),
        "suspense": digest(sorted(store.suspense_items(), key=lambda s: s.suspense_id)),
        "allocations": digest(sorted(
            store.allocations(),
            key=lambda a: (a.bank_line_id, a.installment_id, str(a.amount)))),
        "receivables": digest(sorted(store.receivables(), key=lambda r: r.policy_number)),
        "counts": {
            "packs": len(store.packs()),
            "exceptions": len(store.exceptions()),
            "suspense": len(store.suspense_items()),
            "allocations": len(store.allocations()),
        },
    }


def run_events(store, day=DAY):
    return [
        e.action for e in store.audit_events()
        if e.subject == day.isoformat() and e.action.startswith("day_run_")
    ]


def clean_books():
    store, objects = seeded()
    run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    return books(store)


# ---------------------------------------------------------------------------
# A day runs once


def test_second_run_refused_while_pack_proposed_and_changes_nothing():
    store, objects = seeded()
    run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    before = books(store)
    audit_before = len(store.audit_events())
    with pytest.raises(DayRunError) as refused:
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert "PACK-2026-09-02" in str(refused.value) and "proposed" in str(refused.value)
    assert books(store) == before
    # The refusal is the one trace: exactly one new audit event.
    events = store.audit_events()
    assert len(events) == audit_before + 1 and events[-1].action == "day_run_refused"
    assert "PACK-2026-09-02" in events[-1].detail


def test_second_run_refused_after_approval_pack_stays_approved():
    store, objects = seeded()
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    before = books(store)
    with pytest.raises(DayRunError) as refused:
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert "approved" in str(refused.value)
    assert books(store) == before
    # The approval was not erased: the pack stands approved, and the
    # double application the old overwrite invited cannot happen.
    assert store.pack("PACK-2026-09-02").status.value == "approved"


def test_second_run_refused_after_rejection_rejection_stands():
    store, objects = seeded()
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    reject_pack(store, result["pack_id"], CONTROLLER, "not our reading of the wire")
    before = books(store)
    with pytest.raises(DayRunError) as refused:
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert "rejected" in str(refused.value) and "re-match" in str(refused.value)
    assert books(store) == before
    assert store.pack("PACK-2026-09-02").status.value == "rejected"


def test_day_with_no_lines_runs_once_too():
    store, objects = seeded()
    empty = date(2026, 12, 25)  # nothing lands on Christmas Day
    result = run_day(store, objects, OfflineReader(), POLICY, empty, actor=ASSEMBLER)
    assert result["pack_id"] is None
    with pytest.raises(DayRunError) as refused:
        run_day(store, objects, OfflineReader(), POLICY, empty, actor=ASSEMBLER)
    assert "already ran to completion" in str(refused.value)
    assert run_events(store, empty) == [
        "day_run_started", "day_run_completed", "day_run_refused",
    ]


def test_refusal_over_http_is_a_409_naming_the_pack():
    from fastapi.testclient import TestClient

    from cash_recon.api import Service, create_app

    service = Service(InMemoryStore(), InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    client = TestClient(create_app(service))
    client.post("/api/seed")
    first = client.post("/api/days/2026-09-02/run", json={"actor": "cash-team"})
    assert first.status_code == 200
    second = client.post("/api/days/2026-09-02/run", json={"actor": "cash-team"})
    assert second.status_code == 409
    assert "PACK-2026-09-02" in second.json()["detail"]


def test_refusal_on_the_cli_exits_nonzero_with_the_reason(tmp_path, monkeypatch, capsys):
    from cash_recon import cli

    db = tmp_path / "cli.db"
    monkeypatch.setenv("SQLITE_PATH", str(db))
    monkeypatch.setenv("LLM_BACKEND", "offline")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def invoke(argv):
        monkeypatch.setattr("sys.argv", ["cash-recon", *argv])
        cli.main()

    invoke(["seed"])
    invoke(["run", "2026-09-02"])
    with pytest.raises(SystemExit) as exited:
        invoke(["run", "2026-09-02"])
    assert exited.value.code == 1
    assert "PACK-2026-09-02" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Interrupted runs recover


class CrashOnPackSave(InMemoryStore):
    """Dies in the assemble node, at the day's pack save: persist
    has fully written its exceptions and suspense; no pack exists."""

    def __init__(self, pack_id: str = "PACK-2026-09-02") -> None:
        super().__init__()
        self.armed = True
        self.pack_id = pack_id

    def save_pack(self, pack):
        if self.armed and pack.pack_id == self.pack_id:
            self.armed = False
            raise RuntimeError("simulated crash in assemble")
        super().save_pack(pack)


class CrashMidPersist(InMemoryStore):
    """Dies on the second exception write of the run: half the
    day's exceptions are on the books, half are not."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True
        self.calls = 0

    def add_exception(self, exc):
        if self.armed:
            self.calls += 1
            if self.calls == 2:
                self.armed = False
                raise RuntimeError("simulated crash in persist")
        super().add_exception(exc)


def test_crash_in_assemble_recovers_to_the_clean_books():
    store, objects = seeded(CrashOnPackSave())
    seeded_digest = books(store)["receivables"]
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    # The crash applied nothing — approval is the only applier, and
    # the run never reached a pack, let alone an approval.
    assert books(store)["receivables"] == seeded_digest
    assert store.pack("PACK-2026-09-02") is None

    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True and result["attempt"] == 2
    assert books(store) == clean_books()
    assert run_events(store) == [
        "day_run_started", "day_run_interrupted",
        "day_run_started", "day_run_completed",
    ]
    interrupted = next(
        e for e in store.audit_events() if e.action == "day_run_interrupted")
    assert "attempt 1" in interrupted.detail and "never completed" in interrupted.detail


def test_crash_mid_persist_recovers_to_the_clean_books():
    store, objects = seeded(CrashMidPersist())
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    # Half-persisted: some of the day's exceptions stand, the pack
    # does not, and nothing was applied.
    assert 0 < len(store.exceptions()) < clean_books()["counts"]["exceptions"] + 2
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True
    assert books(store) == clean_books()


def test_crash_in_report_recovers_without_doubling_the_index(monkeypatch):
    store, objects = seeded()
    real_day_report = pipeline.day_report
    state = {"armed": True}

    def flaky(s, day, policy):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("simulated crash in report")
        return real_day_report(s, day, policy)

    monkeypatch.setattr(pipeline, "day_report", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    # The pack stands proposed; the run never completed. This is a
    # recovery, not a refusal — and the allocations index must not
    # double on the way through.
    assert store.pack("PACK-2026-09-02").status.value == "proposed"
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert result["recovered"] is True
    assert books(store) == clean_books()


def test_recovered_day_approves_exactly_once():
    store, objects = seeded(CrashOnPackSave())
    with pytest.raises(RuntimeError):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    result = run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)

    control_store, control_objects = seeded()
    control = run_day(control_store, control_objects, OfflineReader(), POLICY, DAY,
                      actor=ASSEMBLER)
    approve_pack(control_store, control["pack_id"], CONTROLLER, POLICY)
    assert books(store) == books(control_store)


def test_recovery_preserves_workbench_handling_on_the_days_exceptions():
    # 2026-09-04 is a day whose persist writes real exceptions
    # (BL-006 lands short, BL-007 overpays) before assemble dies.
    day = date(2026, 9, 4)
    store, objects = seeded(CrashOnPackSave("PACK-2026-09-04"))
    with pytest.raises(RuntimeError):
        run_day(store, objects, OfflineReader(), POLICY, day, actor=ASSEMBLER)
    # The desk works the crashed attempt's queue before the rerun:
    # an assignment and a note land on one of its exceptions.
    target = next(e for e in store.exceptions(status="open")
                  if e.bank_line_id == "BL-006")
    assign_exception(store, target.exception_id, "Daniel Okafor", CONTROLLER)
    add_handling_note(store, target.exception_id, "Daniel Okafor",
                      "Broker is re-sending the advice; hold for the rerun.")

    run_day(store, objects, OfflineReader(), POLICY, day, actor=ASSEMBLER)
    after = next(e for e in store.exceptions()
                 if e.exception_id == target.exception_id)
    assert after.assignee == "Daniel Okafor"
    assert [n.text for n in after.handling_notes] == [
        "Broker is re-sending the advice; hold for the rerun."]
    assert after.status == "open"


def test_recovery_then_normal_refusal_after_completion():
    store, objects = seeded(CrashMidPersist())
    with pytest.raises(RuntimeError):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    with pytest.raises(DayRunError):
        run_day(store, objects, OfflineReader(), POLICY, DAY, actor=ASSEMBLER)
    assert run_events(store)[-1] == "day_run_refused"
