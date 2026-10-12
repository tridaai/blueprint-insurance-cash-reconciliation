"""The daily report is the control: it foots to the cent or it says
so in words; suspense aging names the oldest cash."""

from datetime import date
from decimal import Decimal

from cash_recon.config import Policy as PolicyConfig
from cash_recon.models import BankLine
from cash_recon.report import day_report, suspense_aging
from tests.conftest import run_through

D = Decimal


def test_day_report_foots(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 8))
    rep = day_report(store, date(2026, 9, 8), PolicyConfig())
    assert rep["bank_total"] == "38849.59"
    assert rep["allocated_total"] == "0.00" and rep["suspense_total"] == "38849.59"
    assert rep["footing_ok"] is True and rep["reconciled"] is True
    codes = {e["code"] for e in rep["exceptions"]}
    assert codes == {"REMITTANCE_ARITHMETIC", "UNIDENTIFIED"}


def test_footing_failure_is_called_out_in_words(world):
    _ds, store, objects = world
    # A bank line nobody worked: the report must refuse to reconcile.
    store.add_bank_line(BankLine(bank_line_id="BL-STRAY", value_date=date(2026, 9, 30),
                                 amount=D("10.00"), payer_name="Somebody", reference=None))
    rep = day_report(store, date(2026, 9, 30), PolicyConfig())
    assert rep["footing_ok"] is False and rep["reconciled"] is False
    assert "NOT reconciled" in rep["footing_note"]


def test_suspense_aging_buckets_and_oldest_named(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 2))
    buckets = {b["bucket"]: b for b in suspense_aging(store, date(2026, 9, 2))}
    assert buckets["31–60 days"]["total"] == "2340.00"  # July seed
    assert buckets["60+ days"]["total"] == "940.00"  # June seed
    oldest = buckets["60+ days"]["items"][0]
    assert oldest["bank_line_id"] == "BL-000A" and oldest["age_days"] == 89


def test_stale_suspense_becomes_an_exception(world):
    _ds, store, objects = world
    run_through(store, objects, date(2026, 9, 2))
    stale = [e for e in store.exceptions(status="open") if e.code.value == "STALE_SUSPENSE"]
    assert {e.bank_line_id for e in stale} == {"BL-000A", "BL-000B"}


def test_report_separates_proposed_from_posted(world):
    _ds, store, objects = world
    from cash_recon.llm import OfflineReader
    from cash_recon.pipeline import run_day

    run_day(store, objects, OfflineReader(), PolicyConfig(), date(2026, 9, 2), actor="cash-team")
    rep = day_report(store, date(2026, 9, 2), PolicyConfig())
    assert rep["allocated_total"] == "18031.60" and rep["posted_total"] == "0.00"
