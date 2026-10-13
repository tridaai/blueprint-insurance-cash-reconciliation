"""Golden-case eval gate for the Insurance Cash Reconciliation Agent.

Runs Northgate's synthetic September AND October through the real
pipeline (offline reader, in-memory stores), approving each day's
packs — the day pack, any non-premium disposition pack, any
combination pack — as a named controller the way the desk would,
then scores every planted case against evals/golden.jsonl — gold by
construction, committed to the repo so the gate grades the pipeline
against a fixed record, not against the dataset builder's current
mood. November — a second currency on the books — runs as a
scenario in its own store (its EUR broker, the governed FX change,
a returned EUR wire re-sent after the move, and the cross-currency
wires that must settle nothing), so the trend watch on the main
books keeps comparing September and October. September's close pack is snapshotted before October is
ingested, so the continuity control (October's opening == September's
closing) grades two independently assembled packs. October also
carries the planted proofs for the combination pass (two bills
settled by pairs of wires, a cent-short near miss and a cross-payer
exact sum that must NOT combine) and the non-premium classifier
(interest, an own transfer, a fee rebate, and a lookalike premium
payment that must not be classified away). The resolution
scenarios run in small worlds of their own: unidentified cash
resolved by its own narrative, an amount two brokers' rates both
explain where only the narrative disambiguates, a wire with no
defensible candidate at all, and a rejected docket — the scan's
candidate set and the gate's choice graded, never a model's
taste. The disposition scenarios run in small worlds of their
own: aged unidentified cash written off under dual control (the
close footing through its disposed term), an escheat-review item
listed but never counted as income, and the refusal matrix —
too young, self-approval, a single approver, a standing
resolution docket, a partially applied item, an open return
chain — each refused in words with the books unchanged. The broker statement case reconciles the shipped example
statement for October — one planted row of each class — against
the pipeline's books, its counts and both footing identities
pinned, with a tampered control total refused whole. The live-mode
remittance-reading set is a separate runner (run_live_evals.py)
and is deliberately not part of this gate.

    python evals/run_evals.py            # score, print the verdict
    python evals/run_evals.py --write    # regenerate golden.jsonl

Exit code is non-zero when the gate fails.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cash_recon.config import Policy as PolicyConfig  # noqa: E402
from cash_recon.dataset import build_dataset, build_november_dataset, build_october_dataset  # noqa: E402
from cash_recon.dispositions import (  # noqa: E402
    DispositionError,
    assemble_disposition_pack,
    disposition_ledger,
)
from cash_recon.gate import approve_pack, reject_pack  # noqa: E402
from cash_recon.llm import OfflineReader  # noqa: E402
from cash_recon.models import (  # noqa: E402
    BankLine,
    Broker,
    ExceptionCode,
    ExceptionRecord,
    Installment,
    PolicyStatus,
    Receivable,
    Remittance,
    ResolutionInfo,
    SuspenseItem,
)
from cash_recon.objects import InMemoryObjectStore  # noqa: E402
from cash_recon.pipeline import (  # noqa: E402
    ingest_dataset,
    ingest_november_dataset,
    ingest_october_dataset,
    run_day,
)
from cash_recon.rematch import rematch_suspense  # noqa: E402
from cash_recon.report import broker_scorecard, day_report, proposal_quality, suspense_aging  # noqa: E402
from cash_recon.resolution import (  # noqa: E402
    ResolutionError,
    assemble_resolution_pack,
    scan_candidates,
)
from cash_recon.store import InMemoryStore  # noqa: E402

GOLDEN = Path(__file__).parent / "golden.jsonl"
CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"


def _november_expect() -> dict:
    """The November case's expectations: the dataset's per-line gold
    (flattened), plus the figures the currency design pins — the
    recorded conversion on each settled line (amount, the governed
    rate in force on the line's own value date, the functional
    amount), the return reversing at the ORIGINAL rate after the
    mid-month move, the per-currency close rolls, and the month-end
    translations. Every figure is computable by hand from the
    dataset and the governed FX table (EUR 1.08 through Nov 15,
    1.10 from Nov 16)."""
    november = build_november_dataset()
    expect: dict = {}
    for bid, gold in sorted(november.gold.items()):
        expect[f"{bid}_tier"] = gold.tier
        expect[f"{bid}_exceptions"] = sorted(gold.exceptions)
        expect[f"{bid}_allocations"] = dict(sorted(gold.allocations.items()))
        expect[f"{bid}_suspense"] = gold.suspense
    expect.update({
        # Recorded conversions, each at its own date's rate: the
        # two netting lines straddle the FX change and convert
        # differently for identical EUR cash.
        "BL-201_fx_rate": "1.08", "BL-201_functional": "12960.00",
        "BL-202_fx_rate": "1.08", "BL-202_functional": "9720.00",
        "BL-207_fx_rate": "1.10", "BL-207_functional": "9900.00",
        "BL-203_fx_rate": "1.10", "BL-203_functional": "9900.00",
        # The re-send links to the returned line; the return was
        # recorded Nov 17, after the rate moved, and reversed the
        # installment in EUR — BL-202's allocation above still
        # stands at its recorded 1.08 (history is not re-priced),
        # and the balance it re-opened is the full billed
        # installment.
        "BL-207_resend_of": "BL-202",
        "return_amount": "9000.00", "return_currency": "EUR",
        "i1_reopened_after_return": "10000.00",
        "i1_status_after_return": "open",
        # Final states: everything Meridian owed is settled, each
        # item in the currency it was billed in.
        "nsp1055_open": "0.00", "nsp1056_open": "0.00", "nsp1057_open": "0.00",
        "nsp1056_i1_applied": "10000.00", "nsp1056_i2_applied": "10000.00",
        # Day reports: the per-currency section and the functional
        # translation, with the day's rates named.
        "nov13_eur_bank": "9000.00", "nov13_eur_allocated": "9000.00",
        "nov13_functional_bank": "9720.00", "nov13_functional_allocated": "9720.00",
        "nov13_rates_used": {"EUR": "1.08"}, "nov13_footing_ok": True,
        "nov14_usd_suspense": "9000.00",
        "nov14_exception_code": "CURRENCY_NO_OPEN_ITEMS",
        "nov14_exception_currency": "USD",
        # The November close: the suspense identity per currency.
        # The USD opening is October's closing (continuity); the
        # only USD suspense received in November is the lookalike
        # wire; the EUR ledger opens at zero and holds BL-204.
        "usd_opening": "85077.74", "usd_received": "9000.00",
        "usd_applied": "0.00", "usd_returned": "0.00",
        "usd_closing": "94077.74", "usd_footed": True,
        "eur_opening": "0.00", "eur_received": "4500.00",
        "eur_applied": "0.00", "eur_returned": "0.00",
        "eur_closing": "4500.00", "eur_footed": True,
        "headline_opening": "85077.74", "headline_closing": "94077.74",
        "close_footed": True,
        "cash_by_currency": {"EUR": "43500.00", "USD": "16200.00"},
        "month_end_rates": {"EUR": "1.10"},
        "closing_translated": {"EUR": "4950.00", "USD": "94077.74"},
        "functional_total": "99027.74",
        "returns_in_month_count": 1,
        "returns_by_currency": {"EUR": "9000.00"},
        "close_digest_stable": True,
        # Trends keep the month's money per currency. (Exception
        # value is pinned for EUR only: November's USD exceptions
        # also carry the STALE_SUSPENSE flags the aging pass raises
        # that month on older USD items — correct, but a figure of
        # the whole book's history, not of November's business.)
        "trend_bank_by_currency": {"EUR": "43500.00", "USD": "16200.00"},
        "trend_allocated_by_currency": {"EUR": "39000.00", "USD": "7200.00"},
        "trend_exception_eur": "4500.00",
        # The scorecard shows Meridian's two denominations apart.
        "meridian_eur_bank": "43500.00", "meridian_eur_allocated": "39000.00",
        "meridian_eur_suspense": "4500.00",
        "meridian_usd_bank": "16200.00", "meridian_usd_allocated": "7200.00",
        "meridian_usd_suspense": "9000.00",
    })
    return expect


def golden_rows() -> list[dict]:
    ds = build_dataset()
    rows: list[dict] = []
    for bid, gold in sorted(ds.gold.items()):
        if bid.startswith("BL-000"):
            continue  # opening suspense seeds are covered by CONTROL-AGING
        rows.append(
            {
                "case": bid,
                "kind": "bank_line",
                "tier": gold.tier,
                "exceptions": sorted(gold.exceptions),
                "allocations": dict(sorted(gold.allocations.items())),
                "suspense": gold.suspense,
                "auto": gold.auto,
                "proposed": gold.proposed,
            }
        )
    for rid, rg in sorted(ds.remittance_gold.items()):
        rows.append(
            {
                "case": rid,
                "kind": "remittance",
                "lines": dict(sorted(rg["lines"].items())),
                "injection": rg["injection"],
                "duplicate_of": rg.get("duplicate_of"),
            }
        )
    october = build_october_dataset()
    for bid, gold in sorted(october.gold.items()):
        rows.append(
            {
                "case": bid,
                "kind": "bank_line",
                "tier": gold.tier,
                "exceptions": sorted(gold.exceptions),
                "allocations": dict(sorted(gold.allocations.items())),
                "suspense": gold.suspense,
                "auto": gold.auto,
                "proposed": gold.proposed,
            }
        )
    october_lines = {l.bank_line_id: l for l in october.bank_lines}
    for bid, sub_kind in sorted(october.non_premium_gold.items()):
        rows.append(
            {
                "case": bid,
                "kind": "non_premium",
                "expect": {
                    "kind": sub_kind,
                    "pack_id": f"NPACK-{october_lines[bid].value_date.isoformat()}",
                    "no_suspense_item": True,
                    "no_allocations": True,
                    "ledger_posted": True,
                },
            }
        )
    rows.append({
        "case": "COMBINATIONS", "kind": "combinations",
        "expect": {
            "packs": ["PACK-COMB-2026-10-22", "PACK-COMB-2026-10-27"],
            "fernway_legs": ["BL-110", "BL-111"],
            "fernway_cash_total": "8784.00",
            "gable_legs": ["BL-108", "BL-109"],
            "gable_cash_total": "5460.00",
            "nsp1050_applied": "6000.00",
            "nsp1051_applied": "10000.00",
            "nsp1050_open": "0.00",
            "nsp1051_open": "0.00",
            "near_miss_legs_open": ["BL-112", "BL-113"],
            "cross_payer_legs_open": ["BL-114", "BL-115"],
            "nsp1052_open": "5000.00",
            "nsp1053_open": "4000.00",
        },
    })
    rows.extend([
        {
            # Planted unidentified cash, resolvable by the wire's
            # own text: the bank mangled the reference ("NSP 7001")
            # past the matcher's strict pattern, so the line sits
            # UNIDENTIFIED — and the governed scan reads the
            # evidence, quotes it, and puts the one candidate to
            # the gate.
            "case": "RESOLUTION-NARRATIVE", "kind": "resolution",
            "expect": {
                "candidates": 1,
                "interpretations": ["narrative"],
                "evidence": ["NSP 7001"],
                "pack_prefix": "PACK-RESOLVE-",
                "item_status": "resolved",
                "exception_status": "resolved",
                "nsp7001_open": "700.00",
                "quality_chosen": 1,
            },
        },
        {
            # The amount foots at TWO brokers' rates; only the
            # narrative says which desk sent it. Gold grades the
            # scan's candidate set and the evidence on it — the
            # wrong-broker candidate must be present and bare —
            # and the pack flow, not any model's taste.
            "case": "RESOLUTION-DISAMBIGUATED", "kind": "resolution",
            "expect": {
                "candidates": 2,
                "policies": ["NSP-7010", "NSP-7011"],
                "evidenced_policy": "NSP-7011",
                "wrong_broker_evidence": "",
                "nsp7011_open": "0.00",
                "nsp7010_open": "10000.00",
                "quality_chosen": 1,
                "quality_lapsed": 1,
            },
        },
        {
            # Nothing fits and nothing is named: the scan comes
            # back empty, assembly refuses in words, the item
            # stays open, and no pack exists.
            "case": "RESOLUTION-NO-CANDIDATE", "kind": "resolution",
            "expect": {
                "candidates": 0,
                "assembly_refused": True,
                "refusal_names_the_gap": True,
                "packs": 0,
                "item_status": "open",
            },
        },
        {
            # A decided docket that was rejected: the item stays
            # open, the candidates stay on the rejected pack's
            # record, and the quality report grades them rejected.
            "case": "RESOLUTION-REJECTED", "kind": "resolution",
            "expect": {
                "candidates_on_record": 2,
                "item_status": "open",
                "chosen_candidate": None,
                "quality_rejected": 2,
            },
        },
    ])
    rows.extend([
        {
            # Aged unidentified cash, written off under dual
            # control: the close roll takes it out through the
            # disposed term, split by kind, and foots.
            "case": "DISPOSITION-WRITE-OFF", "kind": "disposition",
            "expect": {
                "item_status": "dispositioned",
                "disposition_kind": "WRITE_OFF_INCOME",
                "close_opening": "1250.00",
                "close_disposed": "1250.00",
                "close_disposed_write_off": "1250.00",
                "close_disposed_escheat": "0.00",
                "close_closing": "0.00",
                "close_footed": True,
                "aging_open_after": 0,
                "day_decided_write_off": "1250.00",
            },
        },
        {
            # Escheat review: the item is on its own list, in the
            # close's disposed term under its own kind — and never
            # in the write-off (income) figure.
            "case": "DISPOSITION-ESCHEAT", "kind": "disposition",
            "expect": {
                "item_status": "dispositioned",
                "disposition_kind": "ESCHEAT_REVIEW",
                "escheat_listed": ["SUS-BL-E1"],
                "ledger_write_off_items": 0,
                "close_disposed": "640.00",
                "close_disposed_write_off": "0.00",
                "close_disposed_escheat": "640.00",
                "close_footed": True,
            },
        },
        {
            # The refusal matrix: each act refused in words, and
            # the books exactly as they stood — every item open,
            # nothing dispositioned, no pack approved.
            "case": "DISPOSITION-REFUSALS", "kind": "disposition",
            "expect": {
                "too_young_refused": True,
                "self_approval_refused": True,
                "single_approver_refused": True,
                "second_is_assembler_refused": True,
                "docket_refused": True,
                "partial_refused": True,
                "return_chain_refused": True,
                "items_still_open": 5,
                "ledger_items": 0,
                "packs_approved": 0,
            },
        },
    ])
    rows.append({
        "case": "CLOSE-2026-10", "kind": "close_october",
        "expect": {
            "opening_suspense": "64366.99",
            "september_closing_suspense": "64366.99",
            "cash_received_in_month": "113495.09",
            "received_into_suspense": "34954.75",
            "applied_from_suspense": "14244.00",
            "closing_suspense": "85077.74",
            "footed": True,
            "payable_ledger_footed": True,
            "combination_settlements": "14244.00",
        },
    })
    rows.append({
        "case": "RENEWAL-TERM", "kind": "renewal",
        "expect": {
            "installments": 8,
            "named_installment": "NSP-1003-I5",
            "named_status": "applied",
            "named_applied": "2600.00",
            "older_open_installment": "NSP-1003-I2",
            "older_open_balance": "2468.88",
        },
    })
    rows.append({
        "case": "RETURN-PREMIUMS", "kind": "cancellations",
        "expect": {
            "earned_to_the_cent": "2991.78",
            "return_to_insured": "3008.22",
            "payee_insured": "insured",
            "return_to_broker": "5966.85",
            "payee_broker": "Testline Brokers",
            "netted_on_day_report": "3000.00",
            "day_footing_ok": True,
            "payable_after_netting": "8.22",
            "self_approval_refused": True,
            "second_approver_required": True,
            "payout_file_carries_final_row": True,
            "dec_opening_payables": "5966.85",
            "dec_opened": "3008.22",
            "dec_netted": "3000.00",
            "dec_paid": "5975.07",
            "dec_closing_payables": "0.00",
            "dec_payable_ledger_footed": True,
        },
    })
    rows.append({
        "case": "NOVEMBER-MULTICURRENCY", "kind": "november",
        "expect": _november_expect(),
    })
    rows.append({"case": "CONTROL-FOOTING", "kind": "control", "expect": "every day report foots"})
    rows.append({"case": "CONTROL-AGING", "kind": "control", "expect": "seed suspense ages into 31–60 and 60+ buckets"})
    rows.append({
        "case": "REMATCH-LATE-ADVICE", "kind": "rematch",
        "expect": {
            "recovered_bank_line": "BL-T1", "tier": "T4_REMITTANCE",
            "pack_id": "PACK-REMATCH-2026-09-08", "item_status": "resolved",
            "arithmetic_exception": "resolved", "partial_exception": "open",
            "recoveries_total": "4400.00",
        },
    })
    rows.append({
        "case": "REMATCH-STAYS-OPEN", "kind": "rematch",
        "expect": {
            "still_open_reason": "REMITTANCE_ARITHMETIC", "item_status": "open",
            "item_amount": "1000.00", "pack_id": None,
        },
    })
    rows.append({
        "case": "CONTROL-SCORECARD", "kind": "scorecard",
        "expect": {
            "foots_to_daily_reports": True,
            "BRK-COPPER": {"netting_mismatches": 1, "netting_mismatch_delta": "189.60",
                           "clean_netting_rate": None},
            "BRK-ALDER": {"clean_netting_rate": "1.0000", "remittances_received": 2},
        },
    })
    rows.append({
        "case": "CLOSE-2026-09", "kind": "close",
        "expect": {
            "opening_suspense": "3280.00",
            "cash_received_in_month": "219914.65",
            "received_into_suspense": "61086.99",
            "applied_from_suspense": "0.00",
            "approved_write_offs": "0.00",
            "closing_suspense": "64366.99",
            "footed": True,
            "packs_approved": 15,
            "digest_stable": True,
        },
    })
    rows.append({
        "case": "RETURN-NSF-RESEND", "kind": "returns",
        "expect": {
            "reopened_balance": "1000.00", "installment_status": "open",
            "over_return_refused": True, "partial_return_flag": True,
            "applied_after_partial": "600.00", "report_returns_count": 2,
            "report_returns_total": "1000.00", "pack_flagged": True,
            "posting_file_unchanged": True, "resent_tier": "T1_EXACT",
            "resent_duplicate": False, "resent_link": "BL-R1",
            "final_balance": "0.00",
        },
    })
    rows.append({
        "case": "RETURN-SUSPENSE-CLOSE", "kind": "returns_close",
        "expect": {
            "received_into_suspense": "200.00", "returned_from_suspense": "200.00",
            "closing_suspense": "0.00", "footed": True,
            "returns_count": 1, "returns_total": "1200.00",
            "suspense_item_status": "resolved",
        },
    })
    rows.append({
        "case": "WORKBENCH-FLOW", "kind": "workbench",
        "expect": {
            "total_open": 2, "counts_foot": True, "assignee": "Mei-Ling Chen",
            "notes_count": 2, "notes_in_order": True, "off_roster_refused": True,
            "unassigned_codes": ["UNIDENTIFIED"],
        },
    })
    rows.append({
        "case": "PROPOSAL-QUALITY", "kind": "proposals",
        "expect": {
            "remittance_with_edits": 2, "remittance_value_edited": "200.00",
            "lump_sum_as_proposed": 2, "lump_sum_rejected": 2,
            "totals_foot": True,
        },
    })
    rows.append({
        "case": "IMPORT-EXAMPLE", "kind": "import",
        "expect": {
            "imported": 4, "skipped_duplicates": 0, "debit_rows_skipped": 1,
            "credits_total": "13837.60",
            "reimport_imported": 0, "reimport_skipped": 4,
            "lines_on_books": 4, "mismatch_refused": True,
            "lines_after_refusal": 4,
        },
    })
    rows.append({
        "case": "TREND-WATCH", "kind": "trend",
        "expect": {
            "sept_bank_foots": True, "sept_allocated_foots": True,
            "oct_bank_foots": True, "oct_allocated_foots": True,
            "sept_suspense_received": "61086.99",
            "oct_suspense_received": "34954.75",
            "sept_suspense_close_agrees": True,
            "oct_suspense_close_agrees": True,
            "oct_mismatch_value": "100.00",
            "watch_compared": ["2026-09", "2026-10"],
            "watch_status_named": True, "watch_note_explicit": True,
        },
    })
    rows.append({
        "case": "AUDIT-EXPORT", "kind": "line_export",
        "expect": {
            "rich_attempt_stages": ["T4_REMITTANCE"],
            "rich_allocations": 2, "rich_gate_edits": 2,
            "rich_return_id": "RET-BL-Q1", "rich_resent_as": ["BL-Q2"],
            "resend_of": "BL-Q1", "resend_return_link": "RET-BL-Q1",
            "rich_foots": True, "rich_accounted": "4500.00",
            "digest_stable": True, "books_unchanged": True,
            "t1_attempt_stages": ["DUPLICATE_CHECK", "T1_EXACT"],
            "t1_foots": True, "t1_state": "applied",
        },
    })
    rows.append({
        # The broker's own version of October, checked against
        # the books: the shipped example statement carries one
        # planted row of each class (Fernway claims the new 13%
        # retroactively on NSP-1007, overstates NSP-1047's gross,
        # invents NSP-1099, and omits the NSP-1051 combination),
        # and the gold pins the counts, both footing identities,
        # and the refusal of a tampered control total.
        "case": "BROKER-STATEMENT-OCT", "kind": "broker_statement",
        "expect": {
            "counts": {"AGREED": 1, "AMOUNT_DIFFERS": 1, "COMMISSION_DIFFERS": 1,
                       "MISSING_ON_BOOKS": 1, "MISSING_ON_STATEMENT": 1},
            "statement_total": "19061.70",
            "books_total": "25236.90",
            "day_split_allocated": "25236.90",
            "statement_foots": True, "books_foots": True,
            "foots_to_day_splits": True, "foots": True,
            "agreed_policy": "NSP-1046",
            "commission_policy": "NSP-1007",
            "commission_expected": "485.10",
            "amount_policy": "NSP-1047",
            "amount_exception": "EXC-BL-103-OVERPAID",
            "missing_books_policy": "NSP-1099",
            "missing_statement_policy": "NSP-1051",
            "suspense_policy_absent": True,
            "tampered_refused": True,
            "deduped": True,
            "close_cites": True,
            "digest_stable": True,
        },
    })
    return rows


def run_pipeline():
    from cash_recon.close_pack import close_pack_export

    ds = build_dataset()
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, ds)
    reader, policy = OfflineReader(), PolicyConfig()
    days = sorted(
        {l.value_date for l in ds.bank_lines if l.value_date >= date(2026, 9, 1)}
        | {r.received_date for r in ds.remittances}
    )
    outcomes: dict[str, dict] = {}
    reports = {}
    for day in days:
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        for raw in result["outcomes"]:
            outcomes[raw["bank_line_id"]] = raw
        reports[day.isoformat()] = result["report"]
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
        if result.get("non_premium_pack_id"):
            approve_pack(store, result["non_premium_pack_id"], CONTROLLER, policy)
        if result.get("combination_pack_id"):
            approve_pack(store, result["combination_pack_id"], CONTROLLER, policy)
    # September's close is snapshotted BEFORE October lands on the
    # books: the continuity control compares two packs assembled
    # from different states of the world, which is the point.
    september_close = close_pack_export(store, 2026, 9)
    september_close_again = close_pack_export(store, 2026, 9)

    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    for day in sorted({l.value_date for l in october.bank_lines}):
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        for raw in result["outcomes"]:
            outcomes[raw["bank_line_id"]] = raw
        reports[day.isoformat()] = result["report"]
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
        if result.get("non_premium_pack_id"):
            approve_pack(store, result["non_premium_pack_id"], CONTROLLER, policy)
        if result.get("combination_pack_id"):
            approve_pack(store, result["combination_pack_id"], CONTROLLER, policy)
    return ds, store, outcomes, reports, september_close, september_close_again


def _mini_policy(number, premium, broker_id="BRK-TEST"):
    from decimal import Decimal

    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


def _mini_advice(day, lines, total):
    body = [
        "Remittance advice — Testline Brokers",
        "Broker account: BRK-TEST",
        f"Date: {day}",
        "",
        "Premium received for the following policies (net of commission):",
        *[f"Policy {p} — {name} — {amt:,.2f}" for p, name, amt in lines],
        "",
        f"Total remitted: {total:,.2f}",
    ]
    return "\n".join(body)


def combinations_observed(store) -> dict:
    """What the combination pass did on the full books: the packs
    it proposed, the pairs it proved, what approval applied to the
    two target bills, and — as important — the two planted pairs
    it must have left alone (a cent-short near miss and an exact
    sum across two different payers)."""
    comb_packs = sorted(
        p.pack_id for p in store.packs() if p.pack_id.startswith("PACK-COMB")
    )
    legs_by_pack: dict[str, list[str]] = {}
    totals: dict[str, str] = {}
    for pack in store.packs():
        if not pack.pack_id.startswith("PACK-COMB"):
            continue
        legs_by_pack[pack.pack_id] = sorted(
            e.allocation.bank_line_id for e in pack.entries if e.allocation
        )
        totals[pack.pack_id] = str(pack.allocation_total)
    comb_leg_lines = {
        e.allocation.bank_line_id
        for p in store.packs() if p.pack_id.startswith("PACK-COMB")
        for e in p.entries if e.allocation
    }

    def open_items(*line_ids):
        return sorted(
            lid for lid in line_ids
            if any(i.bank_line_id == lid and i.status == "open"
                   for i in store.suspense_items())
            and lid not in comb_leg_lines
        )

    def applied_and_open(policy_number):
        rec = store.receivable(policy_number)
        return str(rec.installments[0].amount_applied), str(rec.open_balance)

    nsp1050_applied, nsp1050_open = applied_and_open("NSP-1050")
    nsp1051_applied, nsp1051_open = applied_and_open("NSP-1051")
    return {
        "packs": comb_packs,
        "fernway_legs": legs_by_pack.get("PACK-COMB-2026-10-22", []),
        "fernway_cash_total": totals.get("PACK-COMB-2026-10-22"),
        "gable_legs": legs_by_pack.get("PACK-COMB-2026-10-27", []),
        "gable_cash_total": totals.get("PACK-COMB-2026-10-27"),
        "nsp1050_applied": nsp1050_applied,
        "nsp1051_applied": nsp1051_applied,
        "nsp1050_open": nsp1050_open,
        "nsp1051_open": nsp1051_open,
        "near_miss_legs_open": open_items("BL-112", "BL-113"),
        "cross_payer_legs_open": open_items("BL-114", "BL-115"),
        "nsp1052_open": str(store.receivable("NSP-1052").open_balance),
        "nsp1053_open": str(store.receivable("NSP-1053").open_balance),
    }


def november_scenario() -> dict:
    """November on its own books — September and October run first,
    so the close has its continuity — in a store of its own: a
    broker billing in EUR, a governed FX change mid-month, an EUR
    wire returned after the move and re-sent, and the two
    cross-currency wires that must settle nothing. Its own store
    because the trend watch on the main books compares September
    and October, and stays that way."""
    from cash_recon.close_pack import close_pack_export
    from cash_recon.config import WatchPolicy
    from cash_recon.returns import record_return
    from cash_recon.trends import match_quality

    ds = build_dataset()
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, ds)
    reader, policy = OfflineReader(), PolicyConfig()
    nov_outcomes: dict[str, dict] = {}
    nov_reports: dict[str, dict] = {}

    def run_approve(day, collect=False):
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
        if result.get("non_premium_pack_id"):
            approve_pack(store, result["non_premium_pack_id"], CONTROLLER, policy)
        if result.get("combination_pack_id"):
            approve_pack(store, result["combination_pack_id"], CONTROLLER, policy)
        if collect:
            for raw in result["outcomes"]:
                nov_outcomes[raw["bank_line_id"]] = raw
            nov_reports[day.isoformat()] = result["report"]
        return result

    september_days = sorted(
        {l.value_date for l in ds.bank_lines if l.value_date >= date(2026, 9, 1)}
        | {r.received_date for r in ds.remittances}
    )
    for day in september_days:
        run_approve(day)
    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    for day in sorted({l.value_date for l in october.bank_lines}):
        run_approve(day)

    november = build_november_dataset()
    ingest_november_dataset(store, objects, november)
    for day in (date(2026, 11, 3), date(2026, 11, 12),
                date(2026, 11, 13), date(2026, 11, 14)):
        run_approve(day, collect=True)
    # The bank takes BL-202 back on Nov 17 — after the governed rate
    # moved on Nov 16. The state is captured here, mid-flow: the
    # installment the reversal re-opened, in EUR.
    ret = record_return(store, "BL-202", "NSF", CONTROLLER, date(2026, 11, 17))
    i1_after_return = store.receivable("NSP-1056").installments[0]
    for day in (date(2026, 11, 18), date(2026, 11, 25), date(2026, 11, 27)):
        run_approve(day, collect=True)

    observed: dict = {}
    for bid in november.gold:
        raw = nov_outcomes[bid]
        agg: dict[str, float] = defaultdict(float)
        for a in raw["allocations"]:
            agg[a["policy_number"]] += float(a["amount"])
        observed[f"{bid}_tier"] = raw["tier"]
        observed[f"{bid}_exceptions"] = sorted(e["code"] for e in raw["exceptions"])
        observed[f"{bid}_allocations"] = {
            k: f"{v:.2f}" for k, v in sorted(agg.items())}
        observed[f"{bid}_suspense"] = f"{float(raw['suspense_amount']):.2f}"
    for bid in ("BL-201", "BL-202", "BL-207", "BL-203"):
        alloc = store.allocations(bid)[0]
        observed[f"{bid}_fx_rate"] = str(alloc.fx_rate)
        observed[f"{bid}_functional"] = str(alloc.functional_amount)
    observed["BL-207_resend_of"] = next(
        l for l in store.bank_lines() if l.bank_line_id == "BL-207").resend_of
    observed["return_amount"] = str(ret.amount)
    observed["return_currency"] = ret.currency
    observed["i1_reopened_after_return"] = str(i1_after_return.open_balance)
    observed["i1_status_after_return"] = i1_after_return.status.value
    rec1056 = store.receivable("NSP-1056")
    observed["nsp1055_open"] = str(store.receivable("NSP-1055").open_balance)
    observed["nsp1056_open"] = str(rec1056.open_balance)
    observed["nsp1057_open"] = str(store.receivable("NSP-1057").open_balance)
    observed["nsp1056_i1_applied"] = str(rec1056.installments[0].amount_applied)
    observed["nsp1056_i2_applied"] = str(rec1056.installments[1].amount_applied)

    rep13 = nov_reports["2026-11-13"]
    observed["nov13_eur_bank"] = rep13["currencies"]["EUR"]["bank"]
    observed["nov13_eur_allocated"] = rep13["currencies"]["EUR"]["allocated"]
    observed["nov13_functional_bank"] = rep13["functional"]["bank_total"]
    observed["nov13_functional_allocated"] = rep13["functional"]["allocated_total"]
    observed["nov13_rates_used"] = rep13["functional"]["rates_used"]
    observed["nov13_footing_ok"] = rep13["footing_ok"]
    rep14 = nov_reports["2026-11-14"]
    observed["nov14_usd_suspense"] = rep14["currencies"]["USD"]["suspense"]
    observed["nov14_exception_code"] = rep14["exceptions"][0]["code"]
    observed["nov14_exception_currency"] = rep14["exceptions"][0]["currency"]

    export = close_pack_export(store, 2026, 11)
    export_again = close_pack_export(store, 2026, 11)
    roll = export["pack"]["control_roll"]
    usd, eur = roll["by_currency"]["USD"], roll["by_currency"]["EUR"]
    observed.update({
        "usd_opening": usd["opening_suspense"],
        "usd_received": usd["received_into_suspense"],
        "usd_applied": usd["applied_from_suspense"],
        "usd_returned": usd["returned_from_suspense"],
        "usd_closing": usd["closing_suspense"], "usd_footed": usd["footed"],
        "eur_opening": eur["opening_suspense"],
        "eur_received": eur["received_into_suspense"],
        "eur_applied": eur["applied_from_suspense"],
        "eur_returned": eur["returned_from_suspense"],
        "eur_closing": eur["closing_suspense"], "eur_footed": eur["footed"],
        "headline_opening": roll["opening_suspense"],
        "headline_closing": roll["closing_suspense"],
        "close_footed": roll["footed"],
        "cash_by_currency": roll["cash_received_in_month_by_currency"],
        "month_end_rates": export["pack"]["functional"]["month_end_rates"],
        "closing_translated": export["pack"]["functional"]["closing_suspense_translated"],
        "functional_total": export["pack"]["functional"]["closing_suspense_functional_total"],
        "returns_in_month_count": roll["returns_in_month"]["count"],
        "returns_by_currency": roll["returns_in_month"]["totals_by_currency"],
        "close_digest_stable": export["sha256"] == export_again["sha256"],
    })
    trend = match_quality(store, WatchPolicy())
    nov = next(m for m in trend["months"] if m["month"] == "2026-11")
    observed["trend_bank_by_currency"] = nov["bank_value_by_currency"]
    observed["trend_allocated_by_currency"] = nov["allocated_value_by_currency"]
    observed["trend_exception_eur"] = nov["exception_value_by_currency"].get("EUR")
    card = {r["broker_id"]: r for r in broker_scorecard(
        store, date(2026, 11, 30), month=(2026, 11))}
    mer = card["BRK-MERIDIAN"]["by_currency"]
    observed["meridian_eur_bank"] = mer["EUR"]["bank_total"]
    observed["meridian_eur_allocated"] = mer["EUR"]["allocated_total"]
    observed["meridian_eur_suspense"] = mer["EUR"]["suspense_total"]
    observed["meridian_usd_bank"] = mer["USD"]["bank_total"]
    observed["meridian_usd_allocated"] = mer["USD"]["allocated_total"]
    observed["meridian_usd_suspense"] = mer["USD"]["suspense_total"]
    return observed


def rematch_scenarios() -> dict[str, dict]:
    """The two planted re-match cases, run in small worlds of their
    own: gold is what the scenario is built to produce."""
    from decimal import Decimal as D

    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))
    policy = PolicyConfig()

    # A wire whose advice does not foot; the corrected advice lands
    # three days later and rescues it.
    store = InMemoryStore()
    store.upsert_broker(broker)
    store.upsert_receivable(_mini_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(_mini_policy("NSP-2002", D("3000.00")))
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-01", broker_id="BRK-TEST", received_date=date(2026, 9, 5),
        stated_total=None,
        text=_mini_advice("2026-09-05", [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                         ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4500.00"))))
    store.add_bank_line(BankLine(bank_line_id="BL-T1", value_date=date(2026, 9, 5),
                                 amount=D("4400.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    run_day(store, InMemoryObjectStore(), OfflineReader(), policy, date(2026, 9, 5), actor=ASSEMBLER)
    store.add_remittance(Remittance(
        remittance_id="REM-TEST-02", broker_id="BRK-TEST", received_date=date(2026, 9, 8),
        stated_total=None,
        text=_mini_advice("2026-09-08", [("NSP-2001", "Alpha Test Co", D("1700.00")),
                                         ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4400.00"))))
    summary = rematch_suspense(store, OfflineReader(), policy, ASSEMBLER, date(2026, 9, 8))
    item = store.suspense_items()[0]
    by_code = {e.code.value: e.status for e in store.exceptions()}
    late_advice = {
        "recovered_bank_line": summary["recovered"][0]["bank_line_id"],
        "tier": summary["recovered"][0]["tier"],
        "pack_id": summary["pack_id"],
        "item_status": item.status,
        "arithmetic_exception": by_code["REMITTANCE_ARITHMETIC"],
        "partial_exception": by_code["PARTIAL"],
        "recoveries_total": day_report(store, date(2026, 9, 8), policy)["rematch_recoveries"]["recovered_total"],
    }

    # An advice arrives claiming the wire's total, but its own lines
    # sum short: the cash stays, under the now-correct code.
    store2 = InMemoryStore()
    store2.upsert_broker(broker)
    store2.upsert_receivable(_mini_policy("NSP-2009", D("1500.00")))
    store2.add_bank_line(BankLine(bank_line_id="BL-T9", value_date=date(2026, 9, 5),
                                  amount=D("1000.00"), payer_name="Testline Brokers",
                                  reference=None, narrative="wire"))
    run_day(store2, InMemoryObjectStore(), OfflineReader(), policy, date(2026, 9, 5), actor=ASSEMBLER)
    store2.add_remittance(Remittance(
        remittance_id="REM-TEST-09", broker_id="BRK-TEST", received_date=date(2026, 9, 7),
        stated_total=None,
        text=_mini_advice("2026-09-07", [("NSP-2009", "Iota Test Co", D("950.00"))], D("1000.00"))))
    summary2 = rematch_suspense(store2, OfflineReader(), policy, ASSEMBLER, date(2026, 9, 7))
    item2 = store2.suspense_items()[0]
    stays_open = {
        "still_open_reason": summary2["still_open"][0]["reason"],
        "item_status": item2.status,
        "item_amount": str(item2.amount),
        "pack_id": summary2["pack_id"],
    }
    return {"REMATCH-LATE-ADVICE": late_advice, "REMATCH-STAYS-OPEN": stays_open}


def resolution_scenarios() -> dict[str, dict]:
    """The planted unidentified-cash cases, in small worlds of
    their own (the pack flow graded, never a model's taste — the
    offline reader ranks nothing, so every figure here is the
    deterministic scan's and the gate's)."""
    from decimal import Decimal as D

    policy = PolicyConfig()
    as_of = date(2026, 10, 6)

    def policy_(number, amounts, broker_id=None, insured=None):
        return Receivable(
            policy_number=number, insured_name=insured or f"Insured {number}",
            broker_id=broker_id, status=PolicyStatus.ACTIVE,
            installments=[Installment(installment_id=f"{number}-I{i + 1}",
                                       policy_number=number, sequence=i + 1,
                                       due_date=date(2026, 10, 1), amount_due=a)
                          for i, a in enumerate(amounts)])

    def park(store, line):
        store.add_bank_line(line)
        store.save_suspense(SuspenseItem(
            suspense_id=f"SUS-{line.bank_line_id}", bank_line_id=line.bank_line_id,
            amount=line.amount, received_date=line.value_date, reason="UNIDENTIFIED"))
        store.add_exception(ExceptionRecord(
            exception_id=f"EXC-{line.bank_line_id}", code=ExceptionCode.UNIDENTIFIED,
            bank_line_id=line.bank_line_id, policy_number=None, broker_id=None,
            amount=line.amount, detail="planted unidentified cash",
            first_seen=line.value_date))
        return f"SUS-{line.bank_line_id}"

    # Case 1: the narrative names the policy (mangled reference).
    store = InMemoryStore()
    store.upsert_receivable(policy_("NSP-7001", [D("2000.00"), D("2000.00")],
                                    insured="Kestrel Point Marina"))
    sid = park(store, BankLine(
        bank_line_id="BL-U1", value_date=date(2026, 10, 5), amount=D("3300.00"),
        payer_name="R G Calloway", reference=None,
        narrative="premium for policy NSP 7001 — balance follows"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, as_of)
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, as_of)
    approve_pack(store, pack.pack_id, CONTROLLER, policy,
                 choice=scan["candidates"][0]["candidate_id"])
    narrative_case = {
        "candidates": len(scan["candidates"]),
        "interpretations": [c["interpretation"] for c in scan["candidates"]],
        "evidence": [c["evidence"] for c in scan["candidates"]],
        "pack_prefix": pack.pack_id.split("-")[0] + "-RESOLVE-",
        "item_status": store.suspense_items()[0].status,
        "exception_status": store.exceptions()[0].status,
        "nsp7001_open": str(store.receivable("NSP-7001").open_balance),
        "quality_chosen": proposal_quality(store)["by_kind"]["resolution"]["chosen"],
    }

    # Case 2: two brokers' rates both foot; the narrative picks one.
    def two_broker_store():
        s = InMemoryStore()
        s.upsert_broker(Broker(broker_id="BRK-A", name="Alder Gate Insurance Brokers",
                               commission_rate=D("0.10")))
        s.upsert_broker(Broker(broker_id="BRK-B", name="Birch & Vale Brokers",
                               commission_rate=D("0.20")))
        s.upsert_receivable(policy_("NSP-7010", [D("10000.00")], "BRK-A"))
        s.upsert_receivable(policy_("NSP-7011", [D("11250.00")], "BRK-B"))
        item = park(s, BankLine(
            bank_line_id="BL-U2", value_date=date(2026, 10, 5), amount=D("9000.00"),
            payer_name="Interstate Payments LLC", reference=None,
            narrative="Birch & Vale remittance — premium"))
        return s, item

    store, sid = two_broker_store()
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, as_of)
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, as_of)
    evidenced = next(c for c in scan["candidates"] if c["evidence"])
    approve_pack(store, pack.pack_id, CONTROLLER, policy,
                 choice=evidenced["candidate_id"])
    quality = proposal_quality(store)["by_kind"]["resolution"]
    disambiguated = {
        "candidates": len(scan["candidates"]),
        "policies": sorted(c["policy_number"] for c in scan["candidates"]),
        "evidenced_policy": evidenced["policy_number"],
        "wrong_broker_evidence": next(
            c["evidence"] for c in scan["candidates"] if not c["evidence"]),
        "nsp7011_open": str(store.receivable("NSP-7011").open_balance),
        "nsp7010_open": str(store.receivable("NSP-7010").open_balance),
        "quality_chosen": quality["chosen"],
        "quality_lapsed": quality["lapsed"],
    }

    # Case 3: nothing fits, nothing is named.
    store = InMemoryStore()
    store.upsert_receivable(policy_("NSP-7004", [D("5000.00")]))
    sid = park(store, BankLine(
        bank_line_id="BL-U3", value_date=date(2026, 10, 5), amount=D("777.77"),
        payer_name="Unknown", reference=None, narrative="premium payment"))
    scan = scan_candidates(store, OfflineReader(), sid, ASSEMBLER, as_of)
    refusal = ""
    try:
        assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, as_of)
    except ResolutionError as exc:
        refusal = str(exc)
    no_candidate = {
        "candidates": len(scan["candidates"]),
        "assembly_refused": bool(refusal),
        "refusal_names_the_gap": "no defensible candidate" in refusal,
        "packs": len(store.packs()),
        "item_status": store.suspense_items()[0].status,
    }

    # Case 4: the docket is rejected; the record keeps the candidates.
    store, sid = two_broker_store()
    pack = assemble_resolution_pack(store, OfflineReader(), sid, ASSEMBLER, as_of)
    reject_pack(store, pack.pack_id, CONTROLLER, "not either desk's money")
    decided = store.pack(pack.pack_id)
    rejected_case = {
        "candidates_on_record": len(decided.resolution.candidates),
        "item_status": store.suspense_items()[0].status,
        "chosen_candidate": decided.resolution.chosen_candidate,
        "quality_rejected": proposal_quality(store)["by_kind"]["resolution"]["rejected"],
    }
    return {
        "RESOLUTION-NARRATIVE": narrative_case,
        "RESOLUTION-DISAMBIGUATED": disambiguated,
        "RESOLUTION-NO-CANDIDATE": no_candidate,
        "RESOLUTION-REJECTED": rejected_case,
    }


def disposition_scenarios() -> dict[str, dict]:
    """The suspense disposition cases, in small worlds of their
    own: the lifecycle's governed end — eligibility in code, dual
    control at the gate, the close footing through the disposed
    term, escheat visibility, and the refusal matrix with the
    books unchanged."""
    from decimal import Decimal as D

    from cash_recon.close_pack import close_pack_export
    from cash_recon.gate import GateError
    from cash_recon.models import Allocation, PostingEntry, PostingPack
    from cash_recon.returns import record_return

    policy = PolicyConfig()
    as_of = date(2026, 10, 20)
    second = "Tomas Reyes"
    aged = date(2026, 6, 15)

    def park(store, line, reason="UNIDENTIFIED", amount=None):
        store.add_bank_line(line)
        store.save_suspense(SuspenseItem(
            suspense_id=f"SUS-{line.bank_line_id}", bank_line_id=line.bank_line_id,
            amount=amount if amount is not None else line.amount,
            received_date=line.value_date, reason=reason, currency=line.currency))
        store.add_exception(ExceptionRecord(
            exception_id=f"EXC-{line.bank_line_id}", code=ExceptionCode.UNIDENTIFIED,
            bank_line_id=line.bank_line_id, policy_number=None, broker_id=None,
            amount=line.amount, currency=line.currency,
            detail="planted unidentified cash", first_seen=line.value_date))
        return f"SUS-{line.bank_line_id}"

    def aged_line(bid, amount, received=aged):
        return BankLine(bank_line_id=bid, value_date=received, amount=D(amount),
                        payer_name="Unknown Remitter", reference=None,
                        narrative="premium payment")

    # Case 1: the write-off, end to end.
    store = InMemoryStore()
    sid = park(store, aged_line("BL-W1", "1250.00"))
    pack = assemble_disposition_pack(
        store, [{"suspense_id": sid, "kind": "WRITE_OFF_INCOME",
                 "reason": "payer untraceable after three letters"}],
        ASSEMBLER, as_of, policy)
    approve_pack(store, pack.pack_id, CONTROLLER, policy, second_approver=second)
    item = store.suspense_items()[0]
    roll = close_pack_export(store, 2026, 10)["pack"]["control_roll"]
    write_off = {
        "item_status": item.status,
        "disposition_kind": item.disposition_kind,
        "close_opening": roll["opening_suspense"],
        "close_disposed": roll["disposed_from_suspense"],
        "close_disposed_write_off": roll["disposed_by_kind"]["WRITE_OFF_INCOME"],
        "close_disposed_escheat": roll["disposed_by_kind"]["ESCHEAT_REVIEW"],
        "close_closing": roll["closing_suspense"],
        "close_footed": roll["footed"],
        "aging_open_after": sum(
            b["count"] for b in suspense_aging(store, date(2026, 11, 1))),
        "day_decided_write_off": day_report(store, as_of, policy)[
            "suspense_dispositions"]["decided_totals_by_kind"].get("WRITE_OFF_INCOME"),
    }

    # Case 2: escheat review — listed, never income.
    store = InMemoryStore()
    sid = park(store, aged_line("BL-E1", "640.00"))
    pack = assemble_disposition_pack(
        store, [{"suspense_id": sid, "kind": "ESCHEAT_REVIEW",
                 "reason": "payer dissolved; funds may be owed to the estate"}],
        ASSEMBLER, as_of, policy)
    approve_pack(store, pack.pack_id, CONTROLLER, policy, second_approver=second)
    ledger = disposition_ledger(store)
    roll = close_pack_export(store, 2026, 10)["pack"]["control_roll"]
    escheat = {
        "item_status": store.suspense_items()[0].status,
        "disposition_kind": store.suspense_items()[0].disposition_kind,
        "escheat_listed": [r["suspense_id"] for r in ledger["escheat_review"]],
        "ledger_write_off_items": len([
            r for r in ledger["items"] if r["kind"] == "WRITE_OFF_INCOME"]),
        "close_disposed": roll["disposed_from_suspense"],
        "close_disposed_write_off": roll["disposed_by_kind"]["WRITE_OFF_INCOME"],
        "close_disposed_escheat": roll["disposed_by_kind"]["ESCHEAT_REVIEW"],
        "close_footed": roll["footed"],
    }

    # Case 3: the refusal matrix.
    store = InMemoryStore()
    young = park(store, aged_line("BL-R1", "300.00", received=date(2026, 9, 25)))
    open_item = park(store, aged_line("BL-R2", "800.00"))
    docketed = park(store, aged_line("BL-R3", "450.00"))
    store.save_pack(PostingPack(
        pack_id="PACK-RESOLVE-2026-10-19-SUS-BL-R3", value_date=date(2026, 10, 19),
        assembled_by=ASSEMBLER,
        resolution=ResolutionInfo(suspense_id=docketed, bank_line_id="BL-R3",
                                  amount=D("450.00"))))

    def partly_applied(bid, item_amount):
        store.upsert_receivable(Receivable(
            policy_number=f"NSP-{bid}", insured_name=f"Insured {bid}",
            broker_id=None, status=PolicyStatus.ACTIVE,
            installments=[Installment(
                installment_id=f"NSP-{bid}-I1", policy_number=f"NSP-{bid}",
                sequence=1, due_date=date(2026, 6, 1), amount_due=D("1000.00"))]))
        line = BankLine(bank_line_id=bid, value_date=aged, amount=D("1000.00"),
                        payer_name="Direct Payer", reference=f"NSP-{bid}",
                        narrative="part payment")
        store.add_bank_line(line)
        day_pack = PostingPack(pack_id=f"PACK-2026-06-15-{bid}", value_date=aged,
                               assembled_by=ASSEMBLER)
        day_pack.entries.append(PostingEntry(kind="allocation", allocation=Allocation(
            bank_line_id=bid, policy_number=f"NSP-{bid}",
            installment_id=f"NSP-{bid}-I1", amount=D("600.00"), tier=None)))
        store.save_pack(day_pack)
        approve_pack(store, day_pack.pack_id, CONTROLLER, policy)
        return park(store, line, reason="PARTIAL", amount=D(item_amount))

    partial = partly_applied("BL-R4", "400.00")
    chained = partly_applied("BL-R5", "400.00")
    record_return(store, "BL-R5", "NSF", CONTROLLER, date(2026, 8, 1),
                  amount=D("200.00"))

    def refused(fn, needle):
        try:
            fn()
        except (DispositionError, GateError) as exc:
            return needle in str(exc)
        return False

    spec = lambda s: [{"suspense_id": s, "kind": "WRITE_OFF_INCOME",
                       "reason": "aged past identification"}]
    standing = assemble_disposition_pack(store, spec(open_item), ASSEMBLER, as_of, policy)
    refusals = {
        "too_young_refused": refused(
            lambda: assemble_disposition_pack(store, spec(young), ASSEMBLER, as_of, policy),
            "90-day"),
        "self_approval_refused": refused(
            lambda: approve_pack(store, standing.pack_id, ASSEMBLER, policy,
                                 second_approver=second),
            "self-approval"),
        "single_approver_refused": refused(
            lambda: approve_pack(store, standing.pack_id, CONTROLLER, policy),
            "dual-control"),
        "second_is_assembler_refused": refused(
            lambda: approve_pack(store, standing.pack_id, CONTROLLER, policy,
                                 second_approver=ASSEMBLER),
            "neither approver"),
        "docket_refused": refused(
            lambda: assemble_disposition_pack(store, spec(docketed), ASSEMBLER, as_of, policy),
            "resolution pack"),
        "partial_refused": refused(
            lambda: assemble_disposition_pack(store, spec(partial), ASSEMBLER, as_of, policy),
            "not wholly unapplied"),
        "return_chain_refused": refused(
            lambda: assemble_disposition_pack(store, spec(chained), ASSEMBLER, as_of, policy),
            "open return chain"),
        "items_still_open": len([
            i for i in store.suspense_items() if i.status == "open"]),
        "ledger_items": len(disposition_ledger(store)["items"]),
        "packs_approved": len([
            p for p in store.packs() if p.status.value == "approved"
            and p.disposition is not None]),
    }
    return {
        "DISPOSITION-WRITE-OFF": write_off,
        "DISPOSITION-ESCHEAT": escheat,
        "DISPOSITION-REFUSALS": refusals,
    }


def returns_scenarios() -> dict[str, dict]:
    """The planted return cases, in small worlds of their own: an
    NSF return that re-opens a settled policy (and the re-send that
    must not die as a duplicate), and an overpaid line whose return
    releases its suspense and still lets the month foot."""
    from decimal import Decimal as D

    from cash_recon import gate
    from cash_recon.close_pack import close_pack_export
    from cash_recon.models import Installment, PolicyStatus, Receivable
    from cash_recon.report import day_report
    from cash_recon.returns import ReturnError, record_return

    policy = PolicyConfig()

    def direct_policy(number, premium, insured):
        return Receivable(
            policy_number=number, insured_name=insured, broker_id=None,
            status=PolicyStatus.ACTIVE,
            installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                      sequence=1, due_date=date(2026, 9, 1), amount_due=premium)])

    def run_approve(store, day):
        result = run_day(store, InMemoryObjectStore(), OfflineReader(), policy, day, actor=ASSEMBLER)
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
        return result

    store = InMemoryStore()
    store.upsert_receivable(direct_policy("NSP-3001", D("1000.00"), "Alpha Return Co"))
    store.add_bank_line(BankLine(bank_line_id="BL-R1", value_date=date(2026, 9, 3),
                                 amount=D("1000.00"), payer_name="Alpha Return Co",
                                 reference="NSP-3001", narrative=""))
    run_approve(store, date(2026, 9, 3))
    pack = store.pack("PACK-2026-09-03")
    posting_before = gate.posting_file(pack)
    # Round 11: part of a line coming back is recorded, not
    # refused — an over-return is what is refused now. The line
    # comes back in two governed steps on the same day: 400.00
    # (a part-return; 600.00 stands applied), then the rest.
    over_refused = False
    try:
        record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5), amount=D("1400.00"))
    except ReturnError:
        over_refused = True
    partial = record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5),
                            amount=D("400.00"))
    applied_after_partial = str(
        store.receivable("NSP-3001").installments[0].amount_applied)
    record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5))
    rec = store.receivable("NSP-3001")
    report = day_report(store, date(2026, 9, 5), policy)
    nsf = {
        "reopened_balance": str(rec.open_balance),
        "installment_status": rec.installments[0].status.value,
        "over_return_refused": over_refused,
        "partial_return_flag": partial.partial,
        "applied_after_partial": applied_after_partial,
        "report_returns_count": report["returns_recorded"]["count"],
        "report_returns_total": report["returns_recorded"]["total"],
        "pack_flagged": store.pack("PACK-2026-09-03").flags == ["RETURN_AFFECTED"],
        "posting_file_unchanged": gate.posting_file(store.pack("PACK-2026-09-03")) == posting_before,
    }
    store.add_bank_line(BankLine(bank_line_id="BL-R2", value_date=date(2026, 9, 6),
                                 amount=D("1000.00"), payer_name="Alpha Return Co",
                                 reference="NSP-3001", narrative=""))
    resent = run_approve(store, date(2026, 9, 6))
    outcome = next(o for o in resent["outcomes"] if o["bank_line_id"] == "BL-R2")
    nsf.update({
        "resent_tier": outcome["tier"],
        "resent_duplicate": any(e["code"] == "DUPLICATE_PAYMENT" for e in outcome["exceptions"]),
        "resent_link": next(l for l in store.bank_lines() if l.bank_line_id == "BL-R2").resend_of,
        "final_balance": str(store.receivable("NSP-3001").open_balance),
    })

    store2 = InMemoryStore()
    store2.upsert_receivable(direct_policy("NSP-3002", D("1000.00"), "Beta Return Co"))
    store2.add_bank_line(BankLine(bank_line_id="BL-R3", value_date=date(2026, 9, 4),
                                  amount=D("1200.00"), payer_name="Beta Return Co",
                                  reference="NSP-3002", narrative=""))
    result2 = run_day(store2, InMemoryObjectStore(), OfflineReader(), policy,
                      date(2026, 9, 4), actor=ASSEMBLER)
    approve_pack(store2, result2["pack_id"], CONTROLLER, policy)
    record_return(store2, "BL-R3", "RECALL", CONTROLLER, date(2026, 9, 8))
    roll = close_pack_export(store2, 2026, 9)["pack"]["control_roll"]
    suspense_close = {
        "received_into_suspense": roll["received_into_suspense"],
        "returned_from_suspense": roll["returned_from_suspense"],
        "closing_suspense": roll["closing_suspense"],
        "footed": roll["footed"],
        "returns_count": roll["returns_in_month"]["count"],
        "returns_total": roll["returns_in_month"]["total"],
        "suspense_item_status": store2.suspense_items()[0].status,
    }
    return {"RETURN-NSF-RESEND": nsf, "RETURN-SUSPENSE-CLOSE": suspense_close}


def cancellation_scenarios() -> dict:
    """The planted return-premium cases, in a small world of their
    own: a direct policy cancelled mid-term (pro-rata to the cent),
    a broker-collected policy whose payable names the broker, cash
    that nets against the payable, and payout packs under the
    gate's rules — closing with a December payable ledger that
    foots on its own identity."""
    from decimal import Decimal as D

    from cash_recon.cancellations import record_cancellation
    from cash_recon.close_pack import close_pack_export
    from cash_recon.gate import GateError
    from cash_recon.payouts import (
        approve_payout_pack,
        assemble_payout_pack,
        payout_file,
    )

    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.upsert_broker(Broker(broker_id="BRK-TEST", name="Testline Brokers",
                               commission_rate=D("0.10")))

    def installments(number, count, premium):
        per = premium / count
        return [Installment(installment_id=f"{number}-I{i + 1}", policy_number=number,
                            sequence=i + 1, due_date=date(2026, 9, 1), amount_due=per)
                for i in range(count)]

    store.upsert_receivable(Receivable(
        policy_number="NSP-6001", insured_name="Insured NSP-6001", broker_id=None,
        status=PolicyStatus.ACTIVE, installments=installments("NSP-6001", 4, D("12000.00")),
        term_start=date(2026, 9, 1), term_end=date(2027, 9, 1)))
    store.upsert_receivable(Receivable(
        policy_number="NSP-6002", insured_name="Insured NSP-6002", broker_id="BRK-TEST",
        status=PolicyStatus.ACTIVE, installments=installments("NSP-6002", 1, D("9000.00")),
        term_start=date(2026, 9, 1), term_end=date(2027, 3, 1)))
    store.add_bank_line(BankLine(bank_line_id="BL-C1", value_date=date(2026, 9, 2),
                                 amount=D("6000.00"), payer_name="Insured NSP-6001",
                                 reference="NSP-6001", narrative=""))
    store.add_bank_line(BankLine(bank_line_id="BL-C3", value_date=date(2026, 9, 2),
                                 amount=D("8100.00"), payer_name="Testline Brokers",
                                 reference="NSP-6002", narrative=""))
    result = run_day(store, objects, OfflineReader(), policy, date(2026, 9, 2), actor=ASSEMBLER)
    approve_pack(store, result["pack_id"], CONTROLLER, policy)

    cxl_b = record_cancellation(store, "NSP-6002", date(2026, 11, 1), CONTROLLER, date(2026, 11, 1))
    cxl_a = record_cancellation(store, "NSP-6001", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))

    store.add_bank_line(BankLine(bank_line_id="BL-C2", value_date=date(2026, 12, 3),
                                 amount=D("3000.00"), payer_name="Insured NSP-6001",
                                 reference="NSP-6001", narrative="installment 3"))
    dec3 = run_day(store, objects, OfflineReader(), policy, date(2026, 12, 3), actor=ASSEMBLER)
    approve_pack(store, dec3["pack_id"], CONTROLLER, policy)
    after_netting = store.payable("PAY-NSP-6001")

    pack_a = assemble_payout_pack(store, date(2026, 12, 4), ["PAY-NSP-6001"], ASSEMBLER)
    self_refused = False
    try:
        approve_payout_pack(store, pack_a.pack_id, ASSEMBLER, policy)
    except GateError:
        self_refused = True
    approve_payout_pack(store, pack_a.pack_id, CONTROLLER, policy)
    file_a = payout_file(store.payout_pack(pack_a.pack_id))

    pack_b = assemble_payout_pack(store, date(2026, 12, 5), ["PAY-NSP-6002"], ASSEMBLER)
    second_required = False
    try:
        approve_payout_pack(store, pack_b.pack_id, CONTROLLER, policy)
    except GateError:
        second_required = True
    approve_payout_pack(store, pack_b.pack_id, CONTROLLER, policy, second_approver="Sofia Marino")

    december = close_pack_export(store, 2026, 12)["pack"]["return_premiums"]
    return {
        "earned_to_the_cent": str(cxl_a.earned_premium),
        "return_to_insured": str(cxl_a.return_premium),
        "payee_insured": cxl_a.payee_kind,
        "return_to_broker": str(cxl_b.return_premium),
        "payee_broker": cxl_b.payee_name,
        "netted_on_day_report": dec3["report"]["netted_total"],
        "day_footing_ok": dec3["report"]["footing_ok"],
        "payable_after_netting": str(after_netting.remaining),
        "self_approval_refused": self_refused,
        "second_approver_required": second_required,
        "payout_file_carries_final_row": "8.22" in file_a and "PAY-NSP-6001" in file_a,
        "dec_opening_payables": december["opening_payables"],
        "dec_opened": december["payables_opened_in_month"],
        "dec_netted": december["netted_in_month"],
        "dec_paid": december["paid_in_month"],
        "dec_closing_payables": december["closing_payables"],
        "dec_payable_ledger_footed": december["footed"],
    }


def workbench_scenario() -> dict:
    """The workbench flow over two planted exceptions: assignment to
    a rostered member, two handling notes in order, an off-roster
    refusal, and counts that foot to the queue."""
    from decimal import Decimal as D

    from cash_recon.models import Installment, PolicyStatus, Receivable
    from cash_recon.workbench import (
        WorkbenchError,
        add_handling_note,
        assign_exception,
        workbench_view,
    )

    policy = PolicyConfig()
    store = InMemoryStore()
    store.upsert_receivable(Receivable(
        policy_number="NSP-4001", insured_name="Gamma Workbench Co", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-4001-I1", policy_number="NSP-4001",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("1000.00"))]))
    store.add_bank_line(BankLine(bank_line_id="BL-W1", value_date=date(2026, 9, 2),
                                 amount=D("500.00"), payer_name="Stranger", reference=None,
                                 narrative=""))
    store.add_bank_line(BankLine(bank_line_id="BL-W2", value_date=date(2026, 9, 3),
                                 amount=D("400.00"), payer_name="Gamma Workbench Co",
                                 reference="NSP-4001", narrative=""))
    objects = InMemoryObjectStore()
    run_day(store, objects, OfflineReader(), policy, date(2026, 9, 2), actor=ASSEMBLER)
    run_day(store, objects, OfflineReader(), policy, date(2026, 9, 3), actor=ASSEMBLER)

    unidentified = next(e for e in store.exceptions(status="open") if e.code.value == "UNIDENTIFIED")
    partial = next(e for e in store.exceptions(status="open") if e.code.value == "PARTIAL")
    refused = False
    try:
        assign_exception(store, unidentified.exception_id, "Some Outsider", actor=ASSEMBLER)
    except WorkbenchError:
        refused = True
    assign_exception(store, partial.exception_id, "Priya Nair", actor=ASSEMBLER)
    unassigned = workbench_view(store, date(2026, 9, 10), unassigned=True)
    assign_exception(store, unidentified.exception_id, "Mei-Ling Chen", actor=ASSEMBLER)
    add_handling_note(store, unidentified.exception_id, "Mei-Ling Chen", "Asked the bank for the payer's full name.")
    add_handling_note(store, unidentified.exception_id, "Tom Alvarez", "Payer identified as a one-off insured; awaiting their reference.")

    view = workbench_view(store, date(2026, 9, 10))
    mine = next(r for r in view["rows"] if r["exception_id"] == unidentified.exception_id)
    return {
        "total_open": view["total_open"],
        "counts_foot": (
            sum(v["count"] for v in view["counts_by_code"].values()) == view["total_open"]
            and sum(D(v["amount"]) for v in view["counts_by_code"].values()) == D(view["total_amount"])
        ),
        "assignee": mine["assignee"],
        "notes_count": len(mine["notes"]),
        "notes_in_order": [n["by"] for n in mine["notes"]] == ["Mei-Ling Chen", "Tom Alvarez"],
        "off_roster_refused": refused,
        "unassigned_codes": [r["code"] for r in unassigned["rows"]],
    }


def proposal_scenario() -> dict:
    """One of each proposal outcome: a remittance read approved
    after a balanced edit, a lump sum accepted as proposed, and a
    lump sum whose pack was rejected — graded from the gate records."""
    from decimal import Decimal as D

    from cash_recon import gate
    from cash_recon.models import Broker, Remittance
    from cash_recon.report import proposal_quality

    policy = PolicyConfig()
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))

    def lump_world():
        s = InMemoryStore()
        s.upsert_broker(broker)
        s.upsert_receivable(_mini_policy("NSP-5001", D("1000.00")))
        s.upsert_receivable(_mini_policy("NSP-5002", D("2000.00")))
        s.add_bank_line(BankLine(bank_line_id="BL-Q9", value_date=date(2026, 9, 5),
                                 amount=D("2700.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire"))
        r = run_day(s, InMemoryObjectStore(), OfflineReader(), policy,
                    date(2026, 9, 5), actor=ASSEMBLER)
        return s, r["pack_id"]

    store = InMemoryStore()
    store.upsert_broker(broker)
    store.upsert_receivable(_mini_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(_mini_policy("NSP-2002", D("3000.00")))
    store.add_remittance(Remittance(
        remittance_id="REM-Q1", broker_id="BRK-TEST", received_date=date(2026, 9, 5),
        stated_total=None,
        text=_mini_advice("2026-09-05", [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                         ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4500.00"))))
    store.add_bank_line(BankLine(bank_line_id="BL-Q1", value_date=date(2026, 9, 5),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    result = run_day(store, InMemoryObjectStore(), OfflineReader(), policy,
                     date(2026, 9, 5), actor=ASSEMBLER)
    pack = store.pack(result["pack_id"])
    idx = {e.allocation.policy_number: i for i, e in enumerate(pack.entries)}
    gate.edit_pack_entries(store, result["pack_id"], CONTROLLER, [
        {"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")},
        {"entry_index": idx["NSP-2002"], "new_amount": D("2800.00")},
    ])
    gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
    quality = proposal_quality(store)
    rem = quality["by_kind"]["remittance_read"]
    decided = sum(
        1 for p in store.packs() if p.status.value in ("approved", "rejected")
        for e in p.entries if e.proposal_kind
    )

    store2, pack2 = lump_world()
    gate.approve_pack(store2, pack2, CONTROLLER, policy)
    lump_clean = proposal_quality(store2)["by_kind"]["lump_sum"]

    store3, pack3 = lump_world()
    gate.reject_pack(store3, pack3, CONTROLLER, "The subset looks wrong; ask the broker.")
    lump_rejected = proposal_quality(store3)["by_kind"]["lump_sum"]

    return {
        "remittance_with_edits": rem["accepted_with_edits"],
        "remittance_value_edited": rem["value_edited"],
        "lump_sum_as_proposed": lump_clean["accepted_as_proposed"],
        "lump_sum_rejected": lump_rejected["rejected"],
        "totals_foot": quality["totals"]["proposals"] == decided == len(quality["proposals"]),
    }


def import_scenario() -> dict:
    """The shipped example statement, imported into a books-only
    store: clean import, dedupe on re-import, and a tampered control
    total refused with nothing further stored."""
    from pathlib import Path

    from cash_recon.statement import StatementImportError, import_statement

    ds = build_dataset()
    store = InMemoryStore()
    for b in ds.brokers:
        store.upsert_broker(b)
    for r in ds.receivables:
        store.upsert_receivable(r)
    text = (Path(__file__).parent.parent / "data" / "example-statement.csv").read_text()
    first = import_statement(store, text, "example-statement.csv")
    second = import_statement(store, text, "example-statement.csv")
    refused = False
    try:
        import_statement(store, text.replace("13837.60", "13837.61"), "tampered.csv")
    except StatementImportError:
        refused = True
    return {
        "imported": first["imported"],
        "skipped_duplicates": first["skipped_duplicates"],
        "debit_rows_skipped": first["debit_rows_skipped"],
        "credits_total": first["credits_total"],
        "reimport_imported": second["imported"],
        "reimport_skipped": second["skipped_duplicates"],
        "lines_on_books": len(store.bank_lines()),
        "mismatch_refused": refused,
        "lines_after_refusal": len(store.bank_lines()),
    }


def line_export_scenario() -> dict:
    """The per-line audit export over a rich line — a remittance
    read, edited at the gate, approved, returned, re-sent — and a
    plain T1 line: the export must trace every cent and every act,
    foot, regenerate byte-identically, and change nothing."""
    from decimal import Decimal as D

    from cash_recon import gate
    from cash_recon.line_export import line_export
    from cash_recon.returns import record_return

    policy = PolicyConfig()
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.upsert_broker(broker)
    store.upsert_receivable(_mini_policy("NSP-2001", D("2000.00")))
    store.upsert_receivable(_mini_policy("NSP-2002", D("3000.00")))
    store.add_remittance(Remittance(
        remittance_id="REM-Q1", broker_id="BRK-TEST", received_date=date(2026, 9, 5),
        stated_total=None,
        text=_mini_advice("2026-09-05", [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                         ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4500.00"))))
    store.add_bank_line(BankLine(bank_line_id="BL-Q1", value_date=date(2026, 9, 5),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    result = run_day(store, objects, OfflineReader(), policy, date(2026, 9, 5), actor=ASSEMBLER)
    pack = store.pack(result["pack_id"])
    idx = {e.allocation.policy_number: i for i, e in enumerate(pack.entries)}
    gate.edit_pack_entries(store, result["pack_id"], CONTROLLER, [
        {"entry_index": idx["NSP-2001"], "new_amount": D("1700.00")},
        {"entry_index": idx["NSP-2002"], "new_amount": D("2800.00")},
    ])
    gate.approve_pack(store, result["pack_id"], CONTROLLER, policy)
    record_return(store, "BL-Q1", "NSF", CONTROLLER, date(2026, 9, 7))
    store.add_remittance(Remittance(
        remittance_id="REM-Q2", broker_id="BRK-TEST", received_date=date(2026, 9, 8),
        stated_total=None,
        text=_mini_advice("2026-09-08", [("NSP-2001", "Alpha Test Co", D("1800.00")),
                                         ("NSP-2002", "Beta Test Co", D("2700.00"))], D("4500.00"))))
    store.add_bank_line(BankLine(bank_line_id="BL-Q2", value_date=date(2026, 9, 8),
                                 amount=D("4500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    resent = run_day(store, objects, OfflineReader(), policy, date(2026, 9, 8), actor=ASSEMBLER)
    gate.approve_pack(store, resent["pack_id"], CONTROLLER, policy)

    before = (len(store.audit_events()), len(store.exceptions()), len(store.packs()))
    first = line_export(store, "BL-Q1")
    again = line_export(store, "BL-Q1")
    after = (len(store.audit_events()), len(store.exceptions()), len(store.packs()))
    body = first["export"]
    resend_body = line_export(store, "BL-Q2")["export"]

    store2 = InMemoryStore()
    store2.upsert_receivable(_mini_policy("NSP-3001", D("1000.00"), broker_id=None))
    store2.add_bank_line(BankLine(bank_line_id="BL-T1", value_date=date(2026, 9, 3),
                                  amount=D("1000.00"), payer_name="Insured NSP-3001",
                                  reference="NSP-3001", narrative=""))
    r2 = run_day(store2, InMemoryObjectStore(), OfflineReader(), policy,
                 date(2026, 9, 3), actor=ASSEMBLER)
    gate.approve_pack(store2, r2["pack_id"], CONTROLLER, policy)
    t1 = line_export(store2, "BL-T1")["export"]

    return {
        "rich_attempt_stages": [s["stage"] for s in body["attempts"][0]["stages"]],
        "rich_allocations": len(body["allocations"]),
        "rich_gate_edits": len(body["gate_history"][0]["edits"]),
        "rich_return_id": body["return"]["return_id"],
        "rich_resent_as": body["resend"]["resent_as"],
        "resend_of": resend_body["resend"]["resend_of"],
        "resend_return_link": resend_body["resend"]["resend_of_return_id"],
        "rich_foots": body["footing"]["foots"],
        "rich_accounted": body["footing"]["accounted"],
        "digest_stable": first["sha256"] == again["sha256"],
        "books_unchanged": before == after,
        "t1_attempt_stages": [s["stage"] for s in t1["attempts"][0]["stages"]],
        "t1_foots": t1["footing"]["foots"],
        "t1_state": t1["final_state"]["state"],
    }


def broker_statement_scenario(store) -> dict:
    """The shipped Fernway example statement for October,
    reconciled against the pipeline's books (September and
    October, every pack approved): one planted row of each class,
    both footing identities, the boundary (BL-104's suspense cash
    raises no row for NSP-1049), a tampered control total refused
    whole, and the October close citing the summary."""
    from pathlib import Path

    from cash_recon.broker_statement import (
        BrokerStatementError,
        broker_reconciliation_export,
        import_broker_statement,
    )
    from cash_recon.close_pack import close_pack_export

    text = Path("examples/example-broker-statement.csv").read_text()
    import_broker_statement(store, text, source_name="example-broker-statement.csv")
    again = import_broker_statement(store, text, source_name="example-broker-statement.csv")
    tampered_refused = False
    try:
        import_broker_statement(store, text.replace("19061.70", "19061.71"))
    except BrokerStatementError:
        tampered_refused = True
    export = broker_reconciliation_export(store, "BRK-FERN", "2026-10", PolicyConfig())
    export_again = broker_reconciliation_export(store, "BRK-FERN", "2026-10", PolicyConfig())
    body = export["reconciliation"]
    rows = {r["policy_number"]: r for r in body["rows"]}
    totals = body["totals"]["USD"]
    by_class = {}
    for cls in ("AGREED", "AMOUNT_DIFFERS", "COMMISSION_DIFFERS",
                "MISSING_ON_BOOKS", "MISSING_ON_STATEMENT"):
        by_class[cls] = next(
            (p for p, r in rows.items() if r["class"] == cls), None)
    citations = close_pack_export(store, 2026, 10)["pack"].get(
        "broker_statement_reconciliation") or []
    return {
        "counts": body["counts"],
        "statement_total": totals["statement_net_total"],
        "books_total": totals["books_net_total"],
        "day_split_allocated": totals["day_split_allocated"],
        "statement_foots": totals["statement_foots"],
        "books_foots": totals["books_foots"],
        "foots_to_day_splits": totals["foots_to_day_splits"],
        "foots": body["foots"],
        "agreed_policy": by_class["AGREED"],
        "commission_policy": by_class["COMMISSION_DIFFERS"],
        "commission_expected": rows["NSP-1007"]["books"]["expected_commission"],
        "amount_policy": by_class["AMOUNT_DIFFERS"],
        "amount_exception": (rows["NSP-1047"]["exception"] or {}).get("exception_id"),
        "missing_books_policy": by_class["MISSING_ON_BOOKS"],
        "missing_statement_policy": by_class["MISSING_ON_STATEMENT"],
        "suspense_policy_absent": "NSP-1049" not in rows,
        "tampered_refused": tampered_refused,
        "deduped": again["deduped"],
        "close_cites": len(citations) == 1 and citations[0]["counts"] == body["counts"]
        and citations[0]["foots"] is True,
        "digest_stable": export["sha256"] == export_again["sha256"],
    }


def score(rows: list[dict]) -> tuple[int, int, list[str]]:
    _ds, store, outcomes, reports, september_close, september_close_again = run_pipeline()
    packs = {p.pack_id: p for p in store.packs()}
    passed, failures = 0, []

    for row in rows:
        case = row["case"]
        if row["kind"] == "bank_line":
            raw = outcomes.get(case)
            if raw is None:
                failures.append(f"{case}: never ran")
                continue
            agg: dict[str, float] = defaultdict(float)
            for a in raw["allocations"]:
                agg[a["policy_number"]] += float(a["amount"])
            got_alloc = {k: f"{v:.2f}" for k, v in sorted(agg.items())}
            got_exc = sorted(e["code"] for e in raw["exceptions"])
            got_susp = f"{float(raw['suspense_amount']):.2f}"
            auto = any(
                entry.auto_applied
                for pack in packs.values()
                for entry in pack.entries
                if entry.allocation and entry.allocation.bank_line_id == case
            )
            proposed = any(a["tier"] is None for a in raw["allocations"]) if raw["allocations"] else False
            problems = []
            if row["tier"] is not None and raw["tier"] != row["tier"]:
                problems.append(f"tier {raw['tier']} != {row['tier']}")
            if got_exc != row["exceptions"]:
                problems.append(f"exceptions {got_exc} != {row['exceptions']}")
            if got_alloc != row["allocations"]:
                problems.append(f"allocations {got_alloc} != {row['allocations']}")
            if got_susp != row["suspense"]:
                problems.append(f"suspense {got_susp} != {row['suspense']}")
            if auto != row["auto"]:
                problems.append(f"auto {auto} != {row['auto']}")
            if proposed != row["proposed"]:
                problems.append(f"proposed {proposed} != {row['proposed']}")
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "remittance":
            rem = next((r for r in store.remittances() if r.remittance_id == case), None)
            if rem is None:
                failures.append(f"{case}: missing")
                continue
            got_lines = {l.policy_number: str(l.amount) for l in rem.lines}
            problems = []
            if got_lines != row["lines"]:
                problems.append(f"lines {got_lines} != {row['lines']}")
            if rem.injection_flagged != row["injection"]:
                problems.append(f"injection {rem.injection_flagged} != {row['injection']}")
            if row.get("duplicate_of"):
                flagged = any(
                    e.action == "near_duplicate_flagged" and e.subject == case
                    for e in store.audit_events()
                )
                if not flagged:
                    problems.append("near-duplicate was not flagged")
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "november":
            observed = november_scenario()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "rematch":
            observed = rematch_scenarios()[case]
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "resolution":
            observed = resolution_scenarios()[case]
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "disposition":
            observed = disposition_scenarios()[case]
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "scorecard":
            from decimal import Decimal as D

            card = {r["broker_id"]: r for r in broker_scorecard(store, date(2026, 9, 21))}
            totals: dict[str, dict[str, D]] = {}
            for day_iso in reports:
                # The unscoped scorecard is cumulative over everything
                # on the books, so it foots to every day's split —
                # September's and October's alike.
                split_by_broker = day_report(store, date.fromisoformat(day_iso), PolicyConfig())["by_broker"]
                for broker_id, split in split_by_broker.items():
                    slot = totals.setdefault(broker_id, {"bank": D("0"), "allocated": D("0"), "suspense": D("0")})
                    for key in slot:
                        slot[key] += D(split[key])
            foots = all(
                D(r["bank_total"]) == totals[r["broker_id"]]["bank"]
                and D(r["allocated_total"]) == totals[r["broker_id"]]["allocated"]
                and D(r["suspense_total"]) == totals[r["broker_id"]]["suspense"]
                for r in card.values()
            )
            observed = {"foots_to_daily_reports": foots}
            for broker_id, fields in row["expect"].items():
                if broker_id == "foots_to_daily_reports":
                    continue
                observed[broker_id] = {k: card[broker_id][k] for k in fields}
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] in ("returns", "returns_close"):
            observed = returns_scenarios()[case]
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "workbench":
            observed = workbench_scenario()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "proposals":
            observed = proposal_scenario()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "import":
            observed = import_scenario()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "trend":
            from decimal import Decimal

            from cash_recon.close_pack import build_close_pack
            from cash_recon.config import WatchPolicy
            from cash_recon.trends import match_quality

            trend_policy = PolicyConfig()
            trend = match_quality(store, WatchPolicy())
            tmonths = {m["month"]: m for m in trend["months"]}

            def day_sums(year, month):
                days = sorted({l.value_date for l in store.bank_lines()
                               if (l.value_date.year, l.value_date.month) == (year, month)})
                bank = sum((Decimal(day_report(store, d, trend_policy)["bank_total"]) for d in days), Decimal("0"))
                alloc = sum((Decimal(day_report(store, d, trend_policy)["allocated_total"]) for d in days), Decimal("0"))
                return bank, alloc

            sept_bank, sept_alloc = day_sums(2026, 9)
            oct_bank, oct_alloc = day_sums(2026, 10)
            sept_roll = build_close_pack(store, 2026, 9)["control_roll"]
            oct_roll = build_close_pack(store, 2026, 10)["control_roll"]
            note = trend["watch"]["note"].lower()
            observed = {
                "sept_bank_foots": Decimal(tmonths["2026-09"]["bank_value"]) == sept_bank,
                "sept_allocated_foots": Decimal(tmonths["2026-09"]["allocated_value"]) == sept_alloc,
                "oct_bank_foots": Decimal(tmonths["2026-10"]["bank_value"]) == oct_bank,
                "oct_allocated_foots": Decimal(tmonths["2026-10"]["allocated_value"]) == oct_alloc,
                "sept_suspense_received": tmonths["2026-09"]["suspense_received_value"],
                "oct_suspense_received": tmonths["2026-10"]["suspense_received_value"],
                "sept_suspense_close_agrees":
                    tmonths["2026-09"]["suspense_received_value"] == sept_roll["received_into_suspense"],
                "oct_suspense_close_agrees":
                    tmonths["2026-10"]["suspense_received_value"] == oct_roll["received_into_suspense"],
                "oct_mismatch_value": tmonths["2026-10"]["exception_mix"]["NETTING_MISMATCH"]["value"],
                "watch_compared": trend["watch"]["compared"],
                "watch_status_named": trend["watch"]["status"] in ("findings", "within tolerance"),
                "watch_note_explicit": "within tolerance" in note or "deterioration" in note,
            }
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "line_export":
            observed = line_export_scenario()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "close":
            export = september_close
            roll = export["pack"]["control_roll"]
            observed = {
                "opening_suspense": roll["opening_suspense"],
                "cash_received_in_month": roll["cash_received_in_month"],
                "received_into_suspense": roll["received_into_suspense"],
                "applied_from_suspense": roll["applied_from_suspense"],
                "approved_write_offs": roll["approved_write_offs"],
                "closing_suspense": roll["closing_suspense"],
                "footed": roll["footed"],
                "packs_approved": len(export["pack"]["packs_approved_in_month"]),
                "digest_stable": export["sha256"] == september_close_again["sha256"],
            }
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "close_october":
            from cash_recon.close_pack import close_pack_export

            export = close_pack_export(store, 2026, 10)
            roll = export["pack"]["control_roll"]
            observed = {
                "opening_suspense": roll["opening_suspense"],
                "september_closing_suspense":
                    september_close["pack"]["control_roll"]["closing_suspense"],
                "cash_received_in_month": roll["cash_received_in_month"],
                "received_into_suspense": roll["received_into_suspense"],
                "applied_from_suspense": roll["applied_from_suspense"],
                "closing_suspense": roll["closing_suspense"],
                "footed": roll["footed"],
                "payable_ledger_footed": export["pack"]["return_premiums"]["footed"],
                "combination_settlements":
                    export["pack"]["combination_settlements"]["settled_total"],
            }
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "non_premium":
            from cash_recon.nonpremium import disposition_for_line, non_premium_ledger

            disp = disposition_for_line(store, case)
            ledger = non_premium_ledger(store)
            observed = {
                "kind": disp["kind"] if disp else None,
                "pack_id": disp["pack_id"] if disp else None,
                "no_suspense_item": not any(
                    i.bank_line_id == case for i in store.suspense_items()
                ),
                "no_allocations": not any(
                    a.bank_line_id == case for a in store.allocations()
                ),
                "ledger_posted": bool(disp) and any(
                    item["bank_line_id"] == case and item["status"] == "approved"
                    for item in ledger["items"]
                ),
            }
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "combinations":
            observed = combinations_observed(store)
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "broker_statement":
            observed = broker_statement_scenario(store)
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "renewal":
            rec = store.receivable("NSP-1003")
            by_id = {i.installment_id: i for i in rec.installments}
            named = by_id["NSP-1003-I5"]
            older = by_id["NSP-1003-I2"]
            observed = {
                "installments": len(rec.installments),
                "named_installment": "NSP-1003-I5",
                "named_status": named.status.value,
                "named_applied": str(named.amount_applied),
                "older_open_installment": "NSP-1003-I2",
                "older_open_balance": str(older.amount_due - older.amount_applied),
            }
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["kind"] == "cancellations":
            observed = cancellation_scenarios()
            problems = [
                f"{k} {observed.get(k)!r} != {v!r}"
                for k, v in row["expect"].items()
                if observed.get(k) != v
            ]
            if problems:
                failures.append(f"{case}: " + "; ".join(problems))
            else:
                passed += 1
        elif row["case"] == "CONTROL-FOOTING":
            bad = [d for d, r in reports.items() if r and not r["footing_ok"]]
            if bad:
                failures.append(f"CONTROL-FOOTING: days that do not foot: {bad}")
            else:
                passed += 1
        elif row["case"] == "CONTROL-AGING":
            # At 2026-09-02 the July seed is 44 days old (31–60) and the
            # June seed is 89 days old (60+); suspense items persist, so
            # the split is checkable after the full run.
            buckets = {b["bucket"]: b for b in suspense_aging(store, date(2026, 9, 2))}
            ok = buckets["31–60 days"]["total"] == "2340.00" and buckets["60+ days"]["total"] == "940.00"
            if ok:
                passed += 1
            else:
                failures.append(f"CONTROL-AGING: buckets {buckets['31–60 days']['total']} / {buckets['60+ days']['total']} != 2340.00 / 940.00")
    return passed, len(rows), failures


def main() -> int:
    if "--write" in sys.argv:
        rows = golden_rows()
        GOLDEN.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        print(f"wrote {len(rows)} golden cases to {GOLDEN}")
        return 0
    rows = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    passed, total, failures = score(rows)
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"\n{passed}/{total} golden cases pass")
    print("GATE: PASS" if passed == total else "GATE: FAIL")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
