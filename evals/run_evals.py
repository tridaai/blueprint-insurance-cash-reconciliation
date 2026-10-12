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
from cash_recon.objects import InMemoryObjectStore  # noqa: E402
from cash_recon.pipeline import ingest_dataset, run_day  # noqa: E402
from cash_recon.report import suspense_aging  # noqa: E402
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
