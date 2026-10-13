"""Multi-currency cash application: matching in currency, conversion
in code, suspense and reports in two denominations.

The unit cases run in small worlds of their own; the November cases
run the shipped November dataset on September + October's books,
the way the eval scenario does, including the returned EUR wire.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cash_recon import fx
from cash_recon.close_pack import build_close_pack
from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset, build_november_dataset, build_october_dataset
from cash_recon.gate import approve_pack
from cash_recon.llm import OfflineReader
from cash_recon.matcher import MatchContext, match_line
from cash_recon.models import (
    BankLine,
    Broker,
    Installment,
    InstallmentStatus,
    PolicyStatus,
    Receivable,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import (
    ingest_dataset,
    ingest_november_dataset,
    ingest_october_dataset,
    run_day,
)
from cash_recon.report import broker_scorecard, day_report, suspense_aging
from cash_recon.returns import record_return
from cash_recon.store import InMemoryStore

D = Decimal
CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"


# ---------------------------------------------------------------------------
# The FX table: governed rates, priced by date

def test_fx_rates_price_by_effective_period():
    assert fx.rate_on("USD", date(2026, 11, 3)) == D("1")
    assert fx.rate_on("EUR", date(2026, 11, 15)) == D("1.08")
    assert fx.rate_on("EUR", date(2026, 11, 16)) == D("1.10")
    # Before the table's first period, the earliest rate stands —
    # the broker-rate-history rule.
    assert fx.rate_on("EUR", date(2025, 6, 1)) == D("1.08")


def test_fx_conversion_rounds_half_up_at_the_one_point():
    assert fx.to_functional(D("9000.00"), "EUR", date(2026, 11, 13)) == D("9720.00")
    # 10.05 EUR at 1.10 = 11.055 -> 11.06 (half-up, at conversion only).
    assert fx.to_functional(D("10.05"), "EUR", date(2026, 11, 20)) == D("11.06")
    assert fx.to_functional(D("10.05"), "USD", date(2026, 11, 20)) == D("10.05")


def test_fx_unknown_currency_is_refused_loudly():
    with pytest.raises(ValueError, match="no governed FX rate table"):
        fx.rate_on("GBP", date(2026, 11, 3))


# ---------------------------------------------------------------------------
# Matching in currency: small worlds

def _world():
    """One broker with an EUR book and a USD book, both open."""
    broker = Broker(broker_id="BRK-EUR", name="Euro Test Brokers",
                    commission_rate=D("0.10"))
    eur = Receivable(
        policy_number="NSP-9001", insured_name="Euro Insured", broker_id="BRK-EUR",
        status=PolicyStatus.ACTIVE, currency="EUR",
        installments=[Installment(installment_id="NSP-9001-I1", policy_number="NSP-9001",
                                  sequence=1, due_date=date(2026, 11, 1),
                                  amount_due=D("10000.00"), currency="EUR")])
    usd = Receivable(
        policy_number="NSP-9002", insured_name="Dollar Insured", broker_id="BRK-EUR",
        status=PolicyStatus.ACTIVE, currency="USD",
        installments=[Installment(installment_id="NSP-9002-I1", policy_number="NSP-9002",
                                  sequence=1, due_date=date(2026, 11, 1),
                                  amount_due=D("8000.00"), currency="USD")])
    ctx = MatchContext(
        receivables={r.policy_number: r for r in (eur, usd)},
        brokers={broker.broker_id: broker},
        remittances=[], prior_lines=[], settled_line_ids=set(),
        proposer=OfflineReader(), policy=PolicyConfig(),
    )
    return ctx


def _line(bid, amount, currency, reference=None, payer="Euro Test Brokers"):
    return BankLine(bank_line_id=bid, value_date=date(2026, 11, 5),
                    amount=amount, payer_name=payer, reference=reference,
                    narrative="", currency=currency)


def test_usd_line_citing_eur_policy_settles_nothing():
    ctx = _world()
    # 9,000.00 USD is exactly the EUR installment's net due. The
    # numbers line up; the currencies do not.
    outcome = match_line(_line("BL-X1", D("9000.00"), "USD", reference="NSP-9001"), ctx)
    assert [e.code.value for e in outcome.exceptions] == ["CURRENCY_NO_OPEN_ITEMS"]
    assert outcome.allocations == []
    assert outcome.suspense_amount == D("9000.00")
    assert ctx.receivables["NSP-9001"].open_balance == D("10000.00")


def test_eur_line_citing_usd_policy_settles_nothing():
    ctx = _world()
    outcome = match_line(_line("BL-X2", D("4500.00"), "EUR", reference="NSP-9002"), ctx)
    assert [e.code.value for e in outcome.exceptions] == ["CURRENCY_NO_OPEN_ITEMS"]
    assert outcome.exceptions[0].currency == "EUR"
    assert outcome.suspense_amount == D("4500.00")


def test_eur_netting_match_records_its_conversion():
    ctx = _world()
    outcome = match_line(_line("BL-X3", D("9000.00"), "EUR", reference="NSP-9001"), ctx)
    assert outcome.tier is not None and outcome.tier.value == "T2_NETTING"
    alloc = outcome.allocations[0]
    assert alloc.currency == "EUR"
    assert alloc.fx_rate == D("1.08")  # Nov 5: the rate in force
    assert alloc.functional_amount == D("9720.00")


def test_usd_match_records_no_conversion():
    ctx = _world()
    outcome = match_line(_line("BL-X4", D("7200.00"), "USD", reference="NSP-9002"), ctx)
    alloc = outcome.allocations[0]
    assert alloc.currency == "USD"
    assert alloc.fx_rate is None and alloc.functional_amount is None


def test_eur_wire_with_only_usd_open_items_is_a_currency_exception():
    ctx = _world()
    # Settle the EUR book, then send an unexplained EUR wire: the
    # broker still has open items — in USD. Not UNIDENTIFIED.
    ctx.receivables["NSP-9001"].installments[0].amount_applied = D("10000.00")
    ctx.receivables["NSP-9001"].installments[0].status = InstallmentStatus.APPLIED
    outcome = match_line(_line("BL-X5", D("1200.00"), "EUR"), ctx)
    assert [e.code.value for e in outcome.exceptions] == ["CURRENCY_NO_OPEN_ITEMS"]
    assert outcome.suspense_amount == D("1200.00")


def test_same_number_other_currency_is_not_a_duplicate():
    ctx = _world()
    eur_line = _line("BL-X6", D("9000.00"), "EUR", reference="NSP-9001")
    match_line(eur_line, ctx)
    ctx.prior_lines = [eur_line]
    ctx.settled_line_ids = {"BL-X6"}
    outcome = match_line(_line("BL-X7", D("9000.00"), "USD", reference="NSP-9002"), ctx)
    assert "DUPLICATE_PAYMENT" not in [e.code.value for e in outcome.exceptions]


# ---------------------------------------------------------------------------
# November on the real books

@pytest.fixture(scope="module")
def november_books():
    """September + October run and approved, November ingested and
    run through Nov 14, BL-202 returned on Nov 17 (state captured),
    then the re-send and the remaining November days."""
    store, objects = InMemoryStore(), InMemoryObjectStore()
    policy = PolicyConfig()
    reader = OfflineReader()
    captured = {}

    def run_approve(day):
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        for pid in (result["pack_id"], result.get("non_premium_pack_id"),
                    result.get("combination_pack_id")):
            if pid:
                approve_pack(store, pid, CONTROLLER, policy)
        return result

    ds = build_dataset()
    ingest_dataset(store, objects, ds)
    days = sorted(
        {l.value_date for l in ds.bank_lines if l.value_date >= date(2026, 9, 1)}
        | {r.received_date for r in ds.remittances}
    )
    for day in days:
        run_approve(day)
    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    for day in sorted({l.value_date for l in october.bank_lines}):
        run_approve(day)
    november = build_november_dataset()
    ingest_november_dataset(store, objects, november)
    reports = {}
    for day in (date(2026, 11, 3), date(2026, 11, 12),
                date(2026, 11, 13), date(2026, 11, 14)):
        reports[day.isoformat()] = run_approve(day)["report"]
    ret = record_return(store, "BL-202", "NSF", CONTROLLER, date(2026, 11, 17))
    captured["return"] = ret
    captured["i1_after_return"] = store.receivable("NSP-1056").installments[0]
    for day in (date(2026, 11, 18), date(2026, 11, 25), date(2026, 11, 27)):
        reports[day.isoformat()] = run_approve(day)["report"]
    return store, policy, reports, captured


def test_november_day_report_is_per_currency(november_books):
    _store, _policy, reports, _cap = november_books
    rep13 = reports["2026-11-13"]
    assert rep13["footing_ok"] is True
    assert rep13["currencies"]["EUR"]["bank"] == "9000.00"
    assert rep13["currencies"]["EUR"]["allocated"] == "9000.00"
    assert rep13["functional"]["bank_total"] == "9720.00"
    assert rep13["functional"]["allocated_total"] == "9720.00"
    assert rep13["functional"]["rates_used"] == {"EUR": "1.08"}
    rep14 = reports["2026-11-14"]
    assert rep14["currencies"]["USD"]["suspense"] == "9000.00"
    assert rep14["exceptions"][0]["code"] == "CURRENCY_NO_OPEN_ITEMS"
    # The EUR installment the USD lookalike cited stands open until
    # BL-203 settles it in EUR on Nov 25 — by month end it is shut.
    assert rep14["footing_ok"] is True


def test_returned_eur_line_reverses_at_the_original_rate(november_books):
    store, _policy, _reports, captured = november_books
    ret = captured["return"]
    assert ret.amount == D("9000.00") and ret.currency == "EUR"
    i1 = captured["i1_after_return"]
    assert i1.open_balance == D("10000.00") and i1.status.value == "open"
    # The original allocation's recorded conversion was NOT
    # re-priced at the Nov 17 rate (1.10): it stands at 1.08.
    alloc = store.allocations("BL-202")[0]
    assert alloc.fx_rate == D("1.08") and alloc.functional_amount == D("9720.00")
    # The re-send is new cash: it settled the same installment at
    # its own date's rate.
    resend = store.allocations("BL-207")[0]
    assert resend.fx_rate == D("1.10") and resend.functional_amount == D("9900.00")
    rec = store.receivable("NSP-1056")
    assert rec.open_balance == D("0.00")


def test_november_close_foots_per_currency(november_books):
    store, _policy, _reports, _cap = november_books
    body = build_close_pack(store, 2026, 11)
    roll = body["control_roll"]
    assert roll["footed"] is True
    usd, eur = roll["by_currency"]["USD"], roll["by_currency"]["EUR"]
    assert (usd["opening_suspense"], usd["received_into_suspense"],
            usd["applied_from_suspense"], usd["returned_from_suspense"],
            usd["closing_suspense"]) == (
                "85077.74", "9000.00", "0.00", "0.00", "94077.74")
    assert usd["footed"] is True
    assert (eur["opening_suspense"], eur["received_into_suspense"],
            eur["applied_from_suspense"], eur["returned_from_suspense"],
            eur["closing_suspense"]) == (
                "0.00", "4500.00", "0.00", "0.00", "4500.00")
    assert eur["footed"] is True
    # The headline roll is the functional currency's.
    assert roll["opening_suspense"] == "85077.74"
    assert roll["closing_suspense"] == "94077.74"
    assert roll["cash_received_in_month_by_currency"] == {
        "EUR": "43500.00", "USD": "16200.00"}
    # Translations at month-end rates, labelled, never posted.
    functional = body["functional"]
    assert functional["month_end_rates"] == {"EUR": "1.10"}
    assert functional["closing_suspense_translated"] == {
        "EUR": "4950.00", "USD": "94077.74"}
    assert functional["closing_suspense_functional_total"] == "99027.74"
    assert body["payment_returns_in_month"][0]["currency"] == "EUR"


def test_november_suspense_aging_and_scorecard_stay_per_currency(november_books):
    store, _policy, _reports, _cap = november_books
    buckets = {b["bucket"]: b for b in suspense_aging(store, date(2026, 11, 30))}
    # BL-204 (EUR, Nov 12) and BL-205 (USD, Nov 14) are both in the
    # 8–30 day bucket at month end — aged side by side, never added.
    bucket = buckets["8–30 days"]
    assert bucket["totals_by_currency"].get("EUR") == "4500.00"
    assert bucket["totals_by_currency"].get("USD") == "9000.00"
    assert {i["bank_line_id"]: i["currency"] for i in bucket["items"]
            if i["bank_line_id"] in ("BL-204", "BL-205")} == {
                "BL-204": "EUR", "BL-205": "USD"}
    card = {r["broker_id"]: r for r in broker_scorecard(
        store, date(2026, 11, 30), month=(2026, 11))}
    mer = card["BRK-MERIDIAN"]["by_currency"]
    assert mer["EUR"] == {"bank_total": "43500.00", "allocated_total": "39000.00",
                          "suspense_total": "4500.00"}
    assert mer["USD"] == {"bank_total": "16200.00", "allocated_total": "7200.00",
                          "suspense_total": "9000.00"}
