"""Golden-case eval gate for the Insurance Cash Reconciliation Agent.

Runs Northgate's synthetic September AND October through the real
pipeline (offline reader, in-memory stores), approving each day's
pack as a named controller the way the desk would, then scores
every planted case against evals/golden.jsonl — gold by
construction, committed to the repo so the gate grades the pipeline
against a fixed record, not against the dataset builder's current
mood. September's close pack is snapshotted before October is
ingested, so the continuity control (October's opening == September's
closing) grades two independently assembled packs.

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
from cash_recon.dataset import build_dataset, build_october_dataset  # noqa: E402
from cash_recon.gate import approve_pack  # noqa: E402
from cash_recon.llm import OfflineReader  # noqa: E402
from cash_recon.models import (  # noqa: E402
    BankLine,
    Broker,
    Installment,
    PolicyStatus,
    Receivable,
    Remittance,
)
from cash_recon.objects import InMemoryObjectStore  # noqa: E402
from cash_recon.pipeline import ingest_dataset, ingest_october_dataset, run_day  # noqa: E402
from cash_recon.rematch import rematch_suspense  # noqa: E402
from cash_recon.report import broker_scorecard, day_report, suspense_aging  # noqa: E402
from cash_recon.store import InMemoryStore  # noqa: E402

GOLDEN = Path(__file__).parent / "golden.jsonl"
CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"


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
    rows.append({
        "case": "CLOSE-2026-10", "kind": "close_october",
        "expect": {
            "opening_suspense": "64366.99",
            "september_closing_suspense": "64366.99",
            "cash_received_in_month": "34753.66",
            "received_into_suspense": "12810.76",
            "closing_suspense": "77177.75",
            "footed": True,
            "payable_ledger_footed": True,
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
            "partial_refused": True, "report_returns_count": 1,
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
    partial_refused = False
    try:
        record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5), amount=D("400.00"))
    except ReturnError:
        partial_refused = True
    record_return(store, "BL-R1", "NSF", CONTROLLER, date(2026, 9, 5))
    rec = store.receivable("NSP-3001")
    report = day_report(store, date(2026, 9, 5), policy)
    nsf = {
        "reopened_balance": str(rec.open_balance),
        "installment_status": rec.installments[0].status.value,
        "partial_refused": partial_refused,
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
                "closing_suspense": roll["closing_suspense"],
                "footed": roll["footed"],
                "payable_ledger_footed": export["pack"]["return_premiums"]["footed"],
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
