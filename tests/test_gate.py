"""The gate: named humans, no self-approval, two approvers for large
write-offs, and receivables that move only on approval."""

from datetime import date
from decimal import Decimal

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import GateError, add_write_off, approve_pack, posting_file, reject_pack
from tests.conftest import ASSEMBLER, CONTROLLER, run_and_approve

D = Decimal


def _run(world, day, approve=True):
    _ds, store, objects = world
    from cash_recon.llm import OfflineReader
    from cash_recon.pipeline import run_day

    result = run_day(store, objects, OfflineReader(), PolicyConfig(), day, actor=ASSEMBLER)
    return store, result


def test_self_approval_is_refused(world):
    store, result = _run(world, date(2026, 9, 2))
    with pytest.raises(GateError, match="self-approval"):
        approve_pack(store, result["pack_id"], ASSEMBLER, PolicyConfig())


def test_approval_applies_allocations_and_closes_the_policy(world):
    _ds, store, objects = world
    run_and_approve(store, objects, date(2026, 9, 2))
    rec = store.receivable("NSP-1001")
    assert rec.status.value == "paid_in_full"
    assert rec.installments[0].amount_applied == D("4800.00")


def test_nothing_moves_before_approval(world):
    store, result = _run(world, date(2026, 9, 2))
    rec = store.receivable("NSP-1001")
    assert rec.status.value == "active" and rec.installments[0].amount_applied == D("0.00")
    assert store.pack(result["pack_id"]).status.value == "proposed"


def test_small_t1_match_is_marked_auto_appliable(world):
    store, result = _run(world, date(2026, 9, 2))
    pack = store.pack(result["pack_id"])
    auto = [e for e in pack.entries if e.auto_applied]
    assert len(auto) == 1 and auto[0].allocation.amount == D("420.00")  # NSP-1019 at/below 500


def _pack_with_partial(world):
    """Run through 2026-09-03 (approved), then run 2026-09-04 leaving
    its pack proposed, with the BL-006 PARTIAL exception on the books."""
    _ds, store, objects = world
    from cash_recon.llm import OfflineReader
    from cash_recon.pipeline import run_day
    from tests.conftest import run_through

    run_through(store, objects, date(2026, 9, 3))
    run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 4), actor=ASSEMBLER)
    return store, "PACK-2026-09-04"


def test_write_off_within_limit_needs_one_approver(world):
    _ds, store, objects = world
    from cash_recon.llm import OfflineReader
    from cash_recon.pipeline import run_day
    from tests.conftest import run_through

    run_through(store, objects, date(2026, 9, 11))
    result = run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 14), actor=ASSEMBLER)
    pack = add_write_off(store, result["pack_id"], "EXC-BL-018-PARTIAL", ASSEMBLER)
    assert pack.write_off_total == D("75.75")  # within the 250.00 single limit
    pack = approve_pack(store, result["pack_id"], CONTROLLER, PolicyConfig())
    assert pack.status.value == "approved" and pack.approvals == [CONTROLLER]


def test_write_off_above_limit_needs_a_second_different_approver(world):
    store, pack_id = _pack_with_partial(world)
    add_write_off(store, pack_id, "EXC-BL-006-PARTIAL", ASSEMBLER)  # 260.00 > 250.00 limit
    with pytest.raises(GateError, match="second approver"):
        approve_pack(store, pack_id, CONTROLLER, PolicyConfig())
    with pytest.raises(GateError, match="second approver must differ"):
        approve_pack(store, pack_id, CONTROLLER, PolicyConfig(), second_approver=CONTROLLER)
    pack = approve_pack(store, pack_id, CONTROLLER, PolicyConfig(), second_approver="Daniel Osei")
    assert pack.status.value == "approved"
    exc = next(e for e in store.exceptions() if e.exception_id == "EXC-BL-006-PARTIAL")
    assert exc.status == "written_off"


def test_write_off_only_for_delta_exceptions(world):
    store, pack_id = _pack_with_partial(world)
    with pytest.raises(GateError, match="only PARTIAL or NETTING_MISMATCH"):
        add_write_off(store, pack_id, "EXC-BL-007-OVERPAID", ASSEMBLER)


def test_rejection_needs_a_reason_and_another_person(world):
    store, result = _run(world, date(2026, 9, 2))
    with pytest.raises(GateError, match="other than the assembler"):
        reject_pack(store, result["pack_id"], ASSEMBLER, "not happy")
    with pytest.raises(GateError, match="reason"):
        reject_pack(store, result["pack_id"], CONTROLLER, "  ")
    pack = reject_pack(store, result["pack_id"], CONTROLLER, "Bank file looks incomplete")
    assert pack.status.value == "rejected"


def test_posting_file_only_for_approved_packs(world):
    store, result = _run(world, date(2026, 9, 2))
    pack = store.pack(result["pack_id"])
    with pytest.raises(GateError, match="approved"):
        posting_file(pack)
    approve_pack(store, result["pack_id"], CONTROLLER, PolicyConfig())
    text = posting_file(store.pack(result["pack_id"]))
    assert "NSP-1001" in text and "4800.00" in text and CONTROLLER in text
