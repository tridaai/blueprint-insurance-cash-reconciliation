"""Demo: Northgate's September, run end to end, offline.

Seeds the synthetic month into in-memory stores, runs every day
through the pipeline, and has a named controller approve each pack —
so the receivables evolve the way they would at a real desk. Prints
the shape of each day and the closing position: the footing, the
exception queue, and the suspense aging an auditor would ask for.
"""

from __future__ import annotations

from datetime import date

from .config import Policy as PolicyConfig
from .dataset import build_dataset
from .gate import approve_pack
from .llm import OfflineReader
from .objects import InMemoryObjectStore
from .pipeline import ingest_dataset, run_day
from .report import day_report, suspense_aging
from .store import InMemoryStore

ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def main() -> None:
    ds = build_dataset()
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, ds)
    reader, policy = OfflineReader(), PolicyConfig()

    days = sorted({l.value_date for l in ds.bank_lines} | {r.received_date for r in ds.remittances})
    print("Northgate Specialty Insurance — September 2026 cash reconciliation (offline demo)\n")
    for day in days:
        if day < date(2026, 9, 1):
            continue
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        rep = result["report"]
        line = (f"{day}  bank {rep['bank_total']:>10}  allocated {rep['allocated_total']:>10}  "
                f"suspense {rep['suspense_total']:>9}  footing {'OK' if rep['footing_ok'] else 'FAIL'}")
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
            line += f"  pack approved by {CONTROLLER}"
        print(line)
        for note in result["document_notes"]:
            print(f"    · {note}")

    print("\nOpen exceptions:")
    for exc in store.exceptions(status="open"):
        print(f"  {exc.code.value:<22} {exc.exception_id:<44} {exc.amount:>10}  {exc.detail[:90]}")

    print("\nSuspense aging at 2026-09-30:")
    for bucket in suspense_aging(store, date(2026, 9, 30)):
        print(f"  {bucket['bucket']:<10} {bucket['total']:>10}  ({bucket['count']} items)")
        for item in bucket["items"]:
            print(f"      {item['bank_line_id']:<8} {item['amount']:>10}  {item['age_days']} days — {item['reason']}")

    final = day_report(store, date(2026, 9, 30), policy)
    print(f"\nFinal word from the control: {final['footing_note']}")


if __name__ == "__main__":
    main()
