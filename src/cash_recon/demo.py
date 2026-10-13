"""Demo: Northgate's September, run end to end, offline.

Seeds the synthetic month, runs every day through the pipeline, and
has a named controller approve each pack — so the receivables evolve
the way they would at a real desk. Prints the shape of each day and
the closing position: the footing, the exception queue, and the
suspense aging an auditor would ask for.

The store is the configured one (Postgres when DATABASE_URL is set,
the SQLITE_PATH file when that is set); with neither configured the
demo uses a throwaway SQLite file, so it exercises the real local
store and still starts clean on every run.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

from .close_pack import close_pack_export
from .config import Policy as PolicyConfig
from .dataset import build_dataset, build_october_dataset
from .gate import approve_pack
from .llm import OfflineReader
from .objects import InMemoryObjectStore
from .pipeline import ingest_dataset, ingest_october_dataset, run_day
from .report import broker_scorecard, day_report, suspense_aging
from .store import SqliteStore, make_store

ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


def _demo_store():
    if os.environ.get("DATABASE_URL") or os.environ.get("SQLITE_PATH"):
        return make_store()
    path = Path(tempfile.mkdtemp(prefix="cash-recon-demo-")) / "cashrecon.db"
    return SqliteStore(path)


def main() -> None:
    ds = build_dataset()
    store, objects = _demo_store(), InMemoryObjectStore()
    where = getattr(store, "path", None)
    print(f"Store: {type(store).__name__}" + (f" at {where}" if where else ""))
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

    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    print("\nOctober 2026 — a second month on the same books "
          "(Fernway's rate changed mid-book; NSP-1003 renewed)\n")
    for day in sorted({l.value_date for l in october.bank_lines}):
        result = run_day(store, objects, reader, policy, day, actor=ASSEMBLER)
        rep = result["report"]
        line = (f"{day}  bank {rep['bank_total']:>10}  allocated {rep['allocated_total']:>10}  "
                f"suspense {rep['suspense_total']:>9}  footing {'OK' if rep['footing_ok'] else 'FAIL'}")
        if result["pack_id"]:
            approve_pack(store, result["pack_id"], CONTROLLER, policy)
            line += f"  pack approved by {CONTROLLER}"
        print(line)

    print("\nOpen exceptions:")
    for exc in store.exceptions(status="open"):
        print(f"  {exc.code.value:<22} {exc.exception_id:<44} {exc.amount:>10}  {exc.detail[:90]}")

    print("\nSuspense aging at 2026-09-30:")
    for bucket in suspense_aging(store, date(2026, 9, 30)):
        print(f"  {bucket['bucket']:<10} {bucket['total']:>10}  ({bucket['count']} items)")
        for item in bucket["items"]:
            print(f"      {item['bank_line_id']:<8} {item['amount']:>10}  {item['age_days']} days — {item['reason']}")

    print("\nBroker scorecard at 2026-09-30:")
    for row in broker_scorecard(store, date(2026, 9, 30)):
        print(f"  {row['broker']:<34} bank {row['bank_total']:>10}  allocated {row['allocated_total']:>10}  "
              f"suspense {row['suspense_total']:>9}  clean netting {row['clean_netting_rate'] or '—'}  "
              f"mismatches {row['netting_mismatches']}")

    final = day_report(store, date(2026, 9, 30), policy)
    print(f"\nFinal word from the control: {final['footing_note']}")

    export = close_pack_export(store, 2026, 9)
    roll = export["pack"]["control_roll"]
    print(f"\nMonth-end close pack for 2026-09: opening suspense {roll['opening_suspense']}, "
          f"received into suspense {roll['received_into_suspense']}, closing {roll['closing_suspense']} "
          f"— {'FOOTS' if roll['footed'] else 'NOT FOOTED'} (SHA-256 {export['sha256'][:16]}…)")


if __name__ == "__main__":
    main()
