"""Golden-case eval gate for the Insurance Cash Reconciliation Agent.

Runs Northgate's synthetic September through the real pipeline
(offline reader, in-memory stores), approving each day's pack as a
named controller the way the desk would, then scores every planted
case against evals/golden.jsonl — gold by construction, committed to
the repo so the gate grades the pipeline against a fixed record, not
against the dataset builder's current mood.

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
from cash_recon.dataset import build_dataset  # noqa: E402
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
from cash_recon.pipeline import ingest_dataset, run_day  # noqa: E402
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
    return rows


def run_pipeline():
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
    return ds, store, outcomes, reports


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


def score(rows: list[dict]) -> tuple[int, int, list[str]]:
    _ds, store, outcomes, reports = run_pipeline()
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
        elif row["kind"] == "close":
            from cash_recon.close_pack import close_pack_export

            export = close_pack_export(store, 2026, 9)
            again = close_pack_export(store, 2026, 9)
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
                "digest_stable": export["sha256"] == again["sha256"],
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
