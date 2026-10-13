"""Non-premium bank lines: classified in code by exact config
rules, disposed at the same gate, never in suspense.

The classifier is the conservative half of the design: every field
a rule names must match, the payer comparison is against the whole
normalised name, and anything else — a payer that merely resembles
the carrier, a bank line whose narrative names no rule's text —
flows through the tiers exactly as before. These tests pin the
classification, the disposition pack's governance (self-approval
refused, named approver, posting file rows), and the reporting
terms (day footing, close pack totals, per-line export).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from cash_recon import gate, nonpremium
from cash_recon.close_pack import build_close_pack
from cash_recon.config import Policy as PolicyConfig
from cash_recon.line_export import line_export
from cash_recon.models import BankLine
from cash_recon.pipeline import ingest_dataset, run_day
from cash_recon.report import day_report
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.store import InMemoryStore

ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def _line(line_id, day, amount, payer, narrative="", reference=None):
    return BankLine(bank_line_id=line_id, value_date=day, amount=D(amount),
                    payer_name=payer, reference=reference, narrative=narrative)


def test_classifier_matches_each_rule_exactly():
    assert nonpremium.classify_line(_line(
        "L1", date(2026, 10, 15), "312.44", "Northgate Bank",
        "Interest credit — premium trust account")) == "INTEREST"
    assert nonpremium.classify_line(_line(
        "L2", date(2026, 10, 29), "85.00", "Northgate Bank",
        "Fee rebate — premium trust account")) == "BANK_FEE"
    assert nonpremium.classify_line(_line(
        "L3", date(2026, 10, 16), "50000.00", "Northgate Specialty Insurance",
        "Transfer in — operating account 4471 to premium trust")) == "OWN_TRANSFER"


def test_classifier_refuses_lookalikes_and_partial_matches():
    # An insured whose name merely resembles the carrier's.
    assert nonpremium.classify_line(_line(
        "L4", date(2026, 10, 30), "6200.00", "Northgate Builders LLC",
        "premium in full", reference="NSP-1054")) is None
    # The bank, but a premium narrative: the rule's text is absent.
    assert nonpremium.classify_line(_line(
        "L5", date(2026, 10, 30), "6200.00", "Northgate Bank",
        "premium payment received")) is None
    # The narrative names interest, but the payer is not the bank.
    assert nonpremium.classify_line(_line(
        "L6", date(2026, 10, 30), "50.00", "Harborlight Insurance Advisors",
        "interest adjustment")) is None
    # An ordinary premium payer, unremarkable in every way.
    assert nonpremium.classify_line(_line(
        "L7", date(2026, 10, 30), "50.00", "Fernway Insurance Group",
        "premium")) is None


def _october_world():
    from cash_recon.dataset import build_dataset, build_october_dataset
    from cash_recon.pipeline import ingest_october_dataset

    ds = build_dataset()
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, ds)
    ingest_october_dataset(store, objects, build_october_dataset())
    return store, objects


def _run(store, objects, day):
    return run_day(store, objects, OfflineReader(), PolicyConfig(), day, actor=ASSEMBLER)


def test_interest_day_routes_out_and_foots():
    store, objects = _october_world()
    result = _run(store, objects, date(2026, 10, 15))
    assert result["non_premium_pack_id"] == "NPACK-2026-10-15"
    assert result["pack_id"] is None  # the day's only line is not premium
    report = day_report(store, date(2026, 10, 15), PolicyConfig())
    assert report["non_premium"]["total"] == "312.44"
    assert report["non_premium"]["items"][0]["kind"] == "INTEREST"
    assert report["footing_ok"] is True
    assert "non-premium" in report["footing_note"]
    # No suspense item, no exception, no aging — ever.
    assert not any(i.bank_line_id == "BL-116" for i in store.suspense_items())
    assert not any(e.bank_line_id == "BL-116" for e in store.exceptions())


def test_disposition_pack_gate_discipline():
    store, objects = _october_world()
    _run(store, objects, date(2026, 10, 16))
    # Self-approval is refused, as at every gate in the blueprint.
    with pytest.raises(gate.GateError, match="self-approval"):
        gate.approve_pack(store, "NPACK-2026-10-16", ASSEMBLER, PolicyConfig())
    gate.approve_pack(store, "NPACK-2026-10-16", CONTROLLER, PolicyConfig())
    ledger = nonpremium.non_premium_ledger(store, month=(2026, 10))
    assert ledger["by_kind"]["OWN_TRANSFER"]["approved"] == "50000.00"
    assert ledger["posted_total"] == "50000.00"
    # The posting file carries the disposition as its own kind.
    text = gate.posting_file(store.pack("NPACK-2026-10-16"))
    assert "non_premium,OWN_TRANSFER" in text or "non_premium" in text
    row = [ln for ln in text.splitlines() if "BL-117" in ln]
    assert row and "50000.00" in row[0]


def test_close_pack_carries_the_months_non_premium_totals():
    store, objects = _october_world()
    for day in (date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 29)):
        result = _run(store, objects, day)
        gate.approve_pack(store, result["non_premium_pack_id"], CONTROLLER, PolicyConfig())
    body = build_close_pack(store, 2026, 10)
    section = body["non_premium"]
    assert section["by_kind"]["INTEREST"]["approved"] == "312.44"
    assert section["by_kind"]["OWN_TRANSFER"]["approved"] == "50000.00"
    assert section["by_kind"]["BANK_FEE"]["approved"] == "85.00"
    assert section["posted_total"] == "50397.44"


def test_line_export_states_the_disposition_and_foots():
    store, objects = _october_world()
    result = _run(store, objects, date(2026, 10, 15))
    gate.approve_pack(store, result["non_premium_pack_id"], CONTROLLER, PolicyConfig())
    export = line_export(store, "BL-116")
    body = export["export"]
    assert body["final_state"]["state"] == "classified non-premium"
    assert body["non_premium"]["kind"] == "INTEREST"
    assert body["footing"]["foots"] is True
    assert body["gate_history"][0]["kind"] == "non_premium_disposition"


def test_ambiguous_line_still_lands_in_unidentified_suspense(world):
    ds, store, objects = world
    store.add_bank_line(_line(
        "BL-AMB", date(2026, 9, 21), "777.00", "Northgate Bank",
        "premium received — policy unknown"))
    result = _run(store, objects, date(2026, 9, 21))
    outcome = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-AMB")
    assert outcome["exceptions"] and str(outcome["suspense_amount"]) == "777.00"
    assert result["non_premium_pack_id"] is None
