"""Installment-aware allocation: each installment is its own open
item, the order is code, and the record says what happened.

- Oldest open installment first, by sequence.
- A reference naming an installment settles that one first; the
  remainder flows to the oldest open installment after it.
- A note indicating another installment changes nothing about where
  the money goes: it applies oldest-first and the outcome carries
  INTENT_DIFFERS for the desk to see.
"""

from datetime import date
from decimal import Decimal

from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.matcher import LineOutcome, MatchContext, match_line
from cash_recon.models import (
    BankLine,
    Installment,
    InstallmentStatus,
    PolicyStatus,
    Receivable,
)
from tests.conftest import run_through

D = Decimal


def _outcome(results, day, bank_line_id):
    for raw in results[date.fromisoformat(day)]["outcomes"]:
        if raw["bank_line_id"] == bank_line_id:
            return LineOutcome.model_validate(raw)
    raise AssertionError(f"{bank_line_id} did not run on {day}")


def _policy(number="NSP-3001", count=4, each=D("1000.00"), applied=()):
    insts = []
    for i in range(count):
        inst = Installment(installment_id=f"{number}-I{i + 1}", policy_number=number,
                           sequence=i + 1, due_date=date(2026, 9, 1), amount_due=each)
        if i + 1 in applied:
            inst.amount_applied = each
            inst.status = InstallmentStatus.APPLIED
        insts.append(inst)
    return Receivable(policy_number=number, insured_name="Installment Test Co",
                      broker_id=None, status=PolicyStatus.ACTIVE, installments=insts)


def _match(rec, amount, reference=None, narrative=""):
    line = BankLine(bank_line_id="BL-T", value_date=date(2026, 9, 20), amount=amount,
                    payer_name="Installment Test Co", reference=reference, narrative=narrative)
    ctx = MatchContext(receivables={rec.policy_number: rec}, brokers={}, remittances=[],
                       prior_lines=[], settled_line_ids=set(), proposer=OfflineReader(),
                       policy=PolicyConfig())
    return match_line(line, ctx)


def _ids(outcome):
    return [(a.installment_id, a.amount) for a in outcome.allocations]


# ---- the planted dataset cases --------------------------------------


def test_named_installment_settles_the_named_one(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 21))
    out = _outcome(results, "2026-09-21", "BL-025")
    assert out.tier.value == "T2_NETTING"
    # Installment 2 is older and open; the payer named 3, so 3 it is.
    assert _ids(out) == [("NSP-1003-I3", D("2221.98"))]
    assert out.allocations[0].note.startswith("Named installment")
    assert out.exceptions == []


def test_intent_differs_applies_oldest_first_and_flags(world):
    _ds, store, objects = world
    results = run_through(store, objects, date(2026, 9, 22))
    out = _outcome(results, "2026-09-22", "BL-026")
    assert _ids(out) == [("NSP-1006-I1", D("3150.06"))]
    assert [e.code.value for e in out.exceptions] == ["INTENT_DIFFERS"]
    exc = out.exceptions[0]
    assert exc.amount == D("3150.06")  # none of it reached installment 2
    assert out.suspense_amount == D("0.00")  # never parked over a guess


# ---- the named path, case by case ------------------------------------


def test_named_remainder_flows_to_oldest_open_after_it():
    out = _match(_policy(), D("2500.00"), reference="NSP-3001-I2")
    assert _ids(out) == [("NSP-3001-I2", D("1000.00")),
                         ("NSP-3001-I3", D("1000.00")),
                         ("NSP-3001-I4", D("500.00"))]
    assert out.tier.value == "T3_REFERENCE"
    assert any("flowed to" in n for n in out.notes)
    assert out.exceptions == [] and out.suspense_amount == D("0.00")


def test_named_partial_stays_on_the_named_installment():
    out = _match(_policy(), D("400.00"), reference="NSP-3001-I3")
    assert _ids(out) == [("NSP-3001-I3", D("400.00"))]
    assert [e.code.value for e in out.exceptions] == ["PARTIAL"]
    assert out.exceptions[0].amount == D("600.00")
    assert out.suspense_amount == D("0.00")


def test_named_exact_is_t1_and_touches_nothing_else():
    out = _match(_policy(), D("1000.00"), reference="NSP-3001-I2")
    assert out.tier.value == "T1_EXACT"
    assert _ids(out) == [("NSP-3001-I2", D("1000.00"))]


def test_named_beyond_the_balance_is_overpaid():
    out = _match(_policy(count=2), D("2500.00"), reference="NSP-3001-I2")
    assert _ids(out) == [("NSP-3001-I2", D("1000.00")), ("NSP-3001-I1", D("1000.00"))]
    assert [e.code.value for e in out.exceptions] == ["OVERPAID"]
    assert out.suspense_amount == D("500.00")


def test_reference_naming_a_settled_installment_falls_back_with_flag():
    out = _match(_policy(applied=(1,)), D("1000.00"), reference="NSP-3001-I1")
    # Installment 1 is settled: the cash applies to the oldest open
    # one, and the difference of intent is on the record.
    assert _ids(out) == [("NSP-3001-I2", D("1000.00"))]
    assert [e.code.value for e in out.exceptions] == ["INTENT_DIFFERS"]


# ---- intent indications that change nothing ---------------------------


def test_intent_matching_the_oldest_open_is_not_flagged():
    out = _match(_policy(), D("1000.00"), reference="NSP-3001",
                 narrative="Installment 1 of 4")
    assert out.tier.value == "T1_EXACT"
    assert out.exceptions == []


def test_intent_flag_counts_only_what_went_elsewhere():
    out = _match(_policy(), D("2000.00"), reference="NSP-3001",
                 narrative="Installment 2 of 4, combined")
    assert _ids(out) == [("NSP-3001-I1", D("1000.00")), ("NSP-3001-I2", D("1000.00"))]
    assert [e.code.value for e in out.exceptions] == ["INTENT_DIFFERS"]
    # Half the payment did reach the indicated installment.
    assert out.exceptions[0].amount == D("1000.00")


def test_allocation_order_is_sequence_not_storage_order():
    rec = _policy()
    rec.installments = list(reversed(rec.installments))  # storage shuffled
    out = _match(rec, D("2500.00"), reference="NSP-3001")
    assert _ids(out) == [("NSP-3001-I1", D("1000.00")),
                         ("NSP-3001-I2", D("1000.00")),
                         ("NSP-3001-I3", D("500.00"))]
