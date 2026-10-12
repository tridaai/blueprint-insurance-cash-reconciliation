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
from .returns import ReturnError, line_history, record_return
from .workbench import WorkbenchError, add_handling_note, assign_exception, workbench_view
from .statement import StatementImportError, import_statement
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
    imp = sub.add_parser("import-statement", help="import a generic bank statement CSV")
    imp.add_argument("path", help="path to the statement CSV")
    ret = sub.add_parser("return", help="record a bank return against an applied bank line")
    ret.add_argument("bank_line_id")
    ret.add_argument("reason", help="NSF | ACCOUNT_CLOSED | STOP_PAYMENT | RECALL")
    ret.add_argument("--actor", required=True, help="who records the return (a named person)")
    ret.add_argument("--on", default=None, help="return date YYYY-MM-DD (default today)")
    ret.add_argument("--amount", default=None,
                     help="amount the bank returned; anything but the full line amount is refused")
    hist = sub.add_parser("history", help="one bank line's whole chain: application, return, re-application")
    hist.add_argument("bank_line_id")
    wb = sub.add_parser("workbench", help="the exception workbench: the open queue, oldest first, counts that foot")
    wb.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    wb.add_argument("--code", default=None)
    wb.add_argument("--broker", default=None)
    wb.add_argument("--assignee", default=None)
    wb.add_argument("--unassigned", action="store_true")
    asg = sub.add_parser("assign", help="assign an open exception to a rostered team member")
    asg.add_argument("exception_id")
    asg.add_argument("assignee")
    asg.add_argument("--actor", required=True)
    nte = sub.add_parser("note", help="append a dated handling note to an exception (append-only)")
    nte.add_argument("exception_id")
    nte.add_argument("text")
    nte.add_argument("--actor", required=True)
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
    elif args.command == "import-statement":
        from pathlib import Path

        try:
            report = import_statement(
                store, Path(args.path).read_text(), source_name=Path(args.path).name
            )
        except StatementImportError as exc:
            parser.exit(1, f"import refused: {exc}\n")
        print(json.dumps(report, indent=2))
    elif args.command == "return":
        from decimal import Decimal

        try:
            recorded = record_return(
                store, args.bank_line_id, args.reason, args.actor,
                date.fromisoformat(args.on) if args.on else date.today(),
                amount=Decimal(args.amount) if args.amount else None,
            )
        except ReturnError as exc:
            parser.exit(1, f"return refused: {exc}\n")
        from .store import dump

        print(json.dumps(json.loads(dump(recorded)), indent=2))
    elif args.command == "history":
        print(json.dumps(line_history(store, args.bank_line_id), indent=2))
    elif args.command == "workbench":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(workbench_view(
            store, as_of, code=args.code, broker_id=args.broker,
            assignee=args.assignee, unassigned=args.unassigned), indent=2))
    elif args.command in ("assign", "note"):
        from .store import dump

        try:
            if args.command == "assign":
                exc = assign_exception(store, args.exception_id, args.assignee, args.actor)
            else:
                exc = add_handling_note(store, args.exception_id, args.actor, args.text)
        except WorkbenchError as exc_err:
            parser.exit(1, f"workbench refused: {exc_err}\n")
        print(json.dumps(json.loads(dump(exc)), indent=2))


if __name__ == "__main__":
    main()
