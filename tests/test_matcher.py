"""The tiers, case by case, plus the accounting invariant every line
must satisfy: allocations + suspense == the bank amount, to the cent."""

from datetime import date
from decimal import Decimal

from cash_recon.matcher import LineOutcome
from tests.conftest import run_through

D = Decimal


def _outcome(results, day, bank_line_id):
    for raw in results[date.fromisoformat(day)]["outcomes"]:
        if raw["bank_line_id"] == bank_line_id:
            return LineOutcome.model_validate(raw)
    raise AssertionError(f"{bank_line_id} did not run on {day}")


def _allocs(outcome):
    agg: dict[str, Decimal] = {}
    for a in outcome.allocations:
        agg[a.policy_number] = agg.get(a.policy_number, Decimal("0.00")) + a.amount
    return agg


def _codes(outcome):
    return sorted(e.code.value for e in outcome.exceptions)


def test_t1_exact_and_t2_netting(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 2))
    t1 = _outcome(results, "2026-09-02", "BL-001")
    assert t1.tier.value == "T1_EXACT" and _allocs(t1) == {"NSP-1001": D("4800.00")}
    t2 = _outcome(results, "2026-09-02", "BL-002")
    assert t2.tier.value == "T2_NETTING" and _allocs(t2) == {"NSP-1021": D("4693.72")}


def test_wrong_rate_netting_is_a_mismatch_not_a_partial(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 3))
    out = _outcome(results, "2026-09-03", "BL-004")
    assert _codes(out) == ["NETTING_MISMATCH"]
    assert out.exceptions[0].amount == D("189.60")  # expected 5372.00, got 5182.40
    assert out.allocations == [] and out.suspense_amount == D("5182.40")


def test_reference_partial_and_overpaid(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 4))
    part = _outcome(results, "2026-09-04", "BL-006")
    assert _codes(part) == ["PARTIAL"] and part.exceptions[0].amount == D("260.00")
    assert _allocs(part) == {"NSP-1037": D("7000.00")}
    over = _outcome(results, "2026-09-04", "BL-007")
    assert _codes(over) == ["OVERPAID"] and _allocs(over) == {"NSP-1010": D("2960.00")}
    assert over.suspense_amount == D("140.00")


def test_remittance_lump_sum_allocates_per_policy(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 7))
    out = _outcome(results, "2026-09-07", "BL-008")
    assert out.tier.value == "T4_REMITTANCE"
    assert _allocs(out) == {
        "NSP-1002": D("10850.00"), "NSP-1011": D("6431.60"),
        "NSP-1020": D("14805.00"), "NSP-1029": D("9165.62"),
    }


def test_remittance_arithmetic_holds_the_whole_wire(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 8))
    out = _outcome(results, "2026-09-08", "BL-010")
    assert _codes(out) == ["REMITTANCE_ARITHMETIC"]
    assert out.allocations == [] and out.suspense_amount == D("36974.59")


def test_unknown_policy_line_inside_a_remittance(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 9))
    out = _outcome(results, "2026-09-09", "BL-012")
    assert _codes(out) == ["UNKNOWN_POLICY"]
    assert _allocs(out) == {"NSP-1005": D("19425.00")}
    assert out.suspense_amount == D("2500.00")


def test_cancelled_policy_cash_is_held(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 9))
    out = _outcome(results, "2026-09-09", "BL-013")
    assert _codes(out) == ["CANCELLED_POLICY"] and out.suspense_amount == D("7280.00")


def test_combined_installments_and_duplicate(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 10))
    combined = _outcome(results, "2026-09-10", "BL-014")
    assert combined.tier.value == "T3_REFERENCE"
    assert len(combined.allocations) == 2
    assert _allocs(combined) == {"NSP-1044": D("3931.20")}
    dup = _outcome(results, "2026-09-10", "BL-015")
    assert _codes(dup) == ["DUPLICATE_PAYMENT"] and dup.allocations == []


def test_lump_sum_proposal_from_the_proposer(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 11))
    out = _outcome(results, "2026-09-11", "BL-017")
    assert out.proposed_by == "offline"
    assert _allocs(out) == {
        "NSP-1018": D("8621.96"), "NSP-1036": D("2905.80"), "NSP-1045": D("10544.40"),
    }


def test_installment_short_and_over(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 15))
    short = _outcome(results, "2026-09-14", "BL-018")
    assert _codes(short) == ["PARTIAL"] and short.exceptions[0].amount == D("75.75")
    over = _outcome(results, "2026-09-15", "BL-020")
    assert _codes(over) == ["OVERPAID"] and _allocs(over) == {"NSP-1033": D("1625.00")}
    assert over.suspense_amount == D("375.00")


def test_unidentified_lines_go_to_suspense(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 17))
    for day, bid in (("2026-09-08", "BL-011"), ("2026-09-17", "BL-023")):
        out = _outcome(results, day, bid)
        assert _codes(out) == ["UNIDENTIFIED"] and out.allocations == []


def test_every_line_accounts_for_every_cent(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 18))
    amounts = {l.bank_line_id: l.amount for l in store.bank_lines()}
    for day_results in results.values():
        for raw in day_results["outcomes"]:
            out = LineOutcome.model_validate(raw)
            assert out.allocated_total + out.suspense_amount == amounts[out.bank_line_id]
