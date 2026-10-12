"""Command line: seed and run days against the configured stores.

With DATABASE_URL / S3_* set this works on the production stores.
Without them the records persist to the local SQLite file
(SQLITE_PATH, default ./data/cashrecon.db) — so `seed`, `run` and
`report` compose across invocations with zero database setup.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from .close_pack import close_pack_export, parse_month, render_close_pack_text
from .config import Policy as PolicyConfig
from .dataset import build_dataset
from .llm import make_reader
from .objects import make_object_store
from .pipeline import ingest_dataset, run_day
from .rematch import rematch_suspense
from .report import broker_scorecard, day_report
from .store import make_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="cash-recon")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="load the synthetic Northgate dataset")
    run = sub.add_parser("run", help="run one day through the pipeline")
    run.add_argument("day", help="YYYY-MM-DD")
    run.add_argument("--actor", default="cash-team")
    rep = sub.add_parser("report", help="print a day's reconciliation report")
    rep.add_argument("day", help="YYYY-MM-DD")
    rem = sub.add_parser("rematch", help="re-match open suspense against the books as they stand")
    rem.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    rem.add_argument("--actor", default="cash-team")
    sco = sub.add_parser("scorecard", help="print the broker netting scorecard")
    sco.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    clo = sub.add_parser("close", help="print a month-end close pack (a report, not a lock)")
    clo.add_argument("month", help="YYYY-MM")
    clo.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()

    store, objects = make_store(), make_object_store()
    policy = PolicyConfig.from_env()
    if args.command == "seed":
        ds = build_dataset()
        ingest_dataset(store, objects, ds)
        print(f"seeded: {len(ds.receivables)} policies, {len(ds.bank_lines)} bank lines, "
              f"{len(ds.remittances)} remittances")
    elif args.command == "run":
        result = run_day(store, objects, make_reader(), policy, date.fromisoformat(args.day), actor=args.actor)
        print(json.dumps({"pack_id": result["pack_id"], "report": result["report"]}, indent=2))
    elif args.command == "report":
        print(json.dumps(day_report(store, date.fromisoformat(args.day), policy), indent=2))
    elif args.command == "rematch":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        summary = rematch_suspense(store, make_reader(), policy, args.actor, as_of)
        print(json.dumps(summary, indent=2))
    elif args.command == "scorecard":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(broker_scorecard(store, as_of), indent=2))
    elif args.command == "close":
        year, month = parse_month(args.month)
        export = close_pack_export(store, year, month)
        if args.format == "text":
            print(render_close_pack_text(export), end="")
        else:
            print(json.dumps(export, indent=2))


if __name__ == "__main__":
    main()
