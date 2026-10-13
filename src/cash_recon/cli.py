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

from .cancellations import CancellationError, record_cancellation
from .close_pack import close_pack_export, parse_month, render_close_pack_text
from .config import WatchPolicy
from .line_export import LineExportError, line_export, render_line_export_text
from .trends import match_quality
from .config import Policy as PolicyConfig
from .dataset import build_dataset
from .gate import GateError
from .llm import make_reader
from .nonpremium import non_premium_ledger
from .objects import make_object_store
from .payouts import approve_payout_pack, assemble_payout_pack, payout_file, reject_payout_pack
from .pipeline import DayRunError, ingest_dataset, run_day
from .rematch import rematch_suspense
from .resolution import ResolutionError, assemble_resolution_pack, scan_candidates
from .report import broker_scorecard, day_report, proposal_quality, return_premiums_view
from .today import today_view
from .returns import ReturnError, line_history, record_return
from .workbench import WorkbenchError, add_handling_note, assign_exception, workbench_view
from .statement import StatementImportError, import_statement
from .store import make_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="cash-recon")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="load the synthetic Northgate dataset")
    tod = sub.add_parser("today", help="the day-open checklist: the desk's morning, in order")
    tod.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    run = sub.add_parser("run", help="run one day through the pipeline")
    run.add_argument("day", help="YYYY-MM-DD")
    run.add_argument("--actor", default="cash-team")
    rep = sub.add_parser("report", help="print a day's reconciliation report")
    rep.add_argument("day", help="YYYY-MM-DD")
    rem = sub.add_parser("rematch", help="re-match open suspense against the books as they stand")
    rem.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    rem.add_argument("--actor", default="cash-team")
    rsc = sub.add_parser("resolve-scan", help="scan one UNIDENTIFIED suspense item for resolution candidates across the whole book")
    rsc.add_argument("suspense_id")
    rsc.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    rsc.add_argument("--actor", default="cash-team")
    rsa = sub.add_parser("resolve-assemble", help="assemble a resolution pack (a docket of alternates) for one UNIDENTIFIED suspense item")
    rsa.add_argument("suspense_id")
    rsa.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    rsa.add_argument("--actor", default="cash-team")
    sco = sub.add_parser("scorecard", help="print the broker netting scorecard")
    sco.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    clo = sub.add_parser("close", help="print a month-end close pack (a report, not a lock)")
    clo.add_argument("month", help="YYYY-MM")
    clo.add_argument("--format", choices=("json", "text"), default="json")
    sub.add_parser("trends", help="the match-quality trend watch: per-month figures, deltas, and named deteriorations (or within tolerance)")
    npl = sub.add_parser("non-premium", help="the non-premium ledger view: classified dispositions by sub-kind, with the gate's state")
    npl.add_argument("--month", help="YYYY-MM; default: all months")
    imp = sub.add_parser("import-statement", help="import a generic bank statement CSV")
    imp.add_argument("path", help="path to the statement CSV")
    ret = sub.add_parser("return", help="record a bank return against an applied bank line")
    ret.add_argument("bank_line_id")
    ret.add_argument("reason", help="NSF | ACCOUNT_CLOSED | STOP_PAYMENT | RECALL")
    ret.add_argument("--actor", required=True, help="who records the return (a named person)")
    ret.add_argument("--on", default=None, help="return date YYYY-MM-DD (default today)")
    ret.add_argument("--amount", default=None,
                     help="amount the bank returned (default: the line's whole standing "
                          "balance); part of the line is recorded as a part-return, and "
                          "more than still stands is refused")
    hist = sub.add_parser("history", help="one bank line's whole chain: application, return, re-application")
    hist.add_argument("bank_line_id")
    aud = sub.add_parser("audit-export", help="the per-line audit export: every cent and every act, digested")
    aud.add_argument("bank_line_id")
    aud.add_argument("--format", choices=("json", "text"), default="json")
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
    sub.add_parser("quality", help="the model proposal quality report: what the gate did with the model's work")
    cxl = sub.add_parser("cancel", help="record a mid-term cancellation; opens the return-premium payable")
    cxl.add_argument("policy_number")
    cxl.add_argument("--effective", required=True, help="cancellation effective date YYYY-MM-DD")
    cxl.add_argument("--actor", required=True, help="who records the cancellation (a named person)")
    cxl.add_argument("--on", default=None, help="recording date YYYY-MM-DD (default today)")
    cxl.add_argument("--reason", default="")
    sub.add_parser("payables", help="the return-premium payables, with their chains")
    asm = sub.add_parser("payout-assemble", help="assemble a payout pack from open payables")
    asm.add_argument("--on", required=True, help="payout value date YYYY-MM-DD")
    asm.add_argument("--payables", required=True, help="comma-separated payable ids")
    asm.add_argument("--actor", required=True, help="who assembles the pack")
    app_ = sub.add_parser("payout-approve", help="approve a payout pack at the gate")
    app_.add_argument("pack_id")
    app_.add_argument("--approver", required=True)
    app_.add_argument("--second", default=None, help="second approver (required above the single-approver limit)")
    rej = sub.add_parser("payout-reject", help="reject a payout pack, with a reason on the record")
    rej.add_argument("pack_id")
    rej.add_argument("--actor", required=True)
    rej.add_argument("--reason", required=True)
    pfile = sub.add_parser("payout-file", help="print an approved payout pack's payout file (CSV)")
    pfile.add_argument("pack_id")
    ck = sub.add_parser("checkpoints", help="the LangGraph checkpoint store: governed retention")
    cksub = ck.add_subparsers(dest="checkpoints_command", required=True)
    prn = cksub.add_parser("prune", help="prune checkpoints beyond the retention policy (config: CHECKPOINT_KEEP_PER_THREAD / CHECKPOINT_MAX_AGE_DAYS)")
    prn.add_argument("--keep", type=int, default=None, help="checkpoints to keep per thread (default: the policy's)")
    prn.add_argument("--max-age-days", type=int, default=None, help="age beyond which kept checkpoints go too — never a thread's newest (default: the policy's)")
    prn.add_argument("--dry-run", action="store_true", help="count what would be pruned; delete nothing")
    args = parser.parse_args()

    if args.command == "checkpoints":
        import dataclasses

        from .checkpoints import prune_checkpoints

        ck_policy = PolicyConfig.from_env()
        overrides = {}
        if args.keep is not None:
            overrides["checkpoint_keep_per_thread"] = args.keep
        if args.max_age_days is not None:
            overrides["checkpoint_max_age_days"] = args.max_age_days
        if overrides:
            ck_policy = dataclasses.replace(ck_policy, **overrides)
        print(json.dumps(prune_checkpoints(ck_policy, dry_run=args.dry_run), indent=2))
        return

    store, objects = make_store(), make_object_store()
    policy = PolicyConfig.from_env()
    if args.command == "today":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(today_view(store, as_of, policy), indent=2))
    elif args.command == "seed":
        ds = build_dataset()
        ingest_dataset(store, objects, ds)
        print(f"seeded: {len(ds.receivables)} policies, {len(ds.bank_lines)} bank lines, "
              f"{len(ds.remittances)} remittances")
    elif args.command == "run":
        try:
            result = run_day(store, objects, make_reader(), policy, date.fromisoformat(args.day), actor=args.actor)
        except DayRunError as exc:
            parser.exit(1, f"run refused: {exc}\n")
        print(json.dumps({"pack_id": result["pack_id"], "report": result["report"]}, indent=2))
    elif args.command == "report":
        print(json.dumps(day_report(store, date.fromisoformat(args.day), policy), indent=2))
    elif args.command == "rematch":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        summary = rematch_suspense(store, make_reader(), policy, args.actor, as_of)
        print(json.dumps(summary, indent=2))
    elif args.command == "resolve-scan":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        try:
            scan = scan_candidates(store, make_reader(), args.suspense_id, args.actor, as_of)
        except ResolutionError as exc:
            parser.exit(1, f"scan refused: {exc}\n")
        print(json.dumps(scan, indent=2))
    elif args.command == "resolve-assemble":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        try:
            pack = assemble_resolution_pack(store, make_reader(), args.suspense_id, args.actor, as_of)
        except ResolutionError as exc:
            parser.exit(1, f"assembly refused: {exc}\n")
        print(json.dumps(json.loads(pack.model_dump_json()), indent=2))
    elif args.command == "scorecard":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(broker_scorecard(store, as_of), indent=2))
    elif args.command == "trends":
        print(json.dumps(match_quality(store, WatchPolicy.from_env()), indent=2))
    elif args.command == "non-premium":
        month = parse_month(args.month) if args.month else None
        print(json.dumps(non_premium_ledger(store, month=month), indent=2))
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
    elif args.command == "audit-export":
        try:
            export = line_export(store, args.bank_line_id)
        except LineExportError as exc:
            parser.exit(1, f"audit export refused: {exc}\n")
        if args.format == "text":
            print(render_line_export_text(export), end="")
        else:
            print(json.dumps(export, indent=2))
    elif args.command == "workbench":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(workbench_view(
            store, as_of, code=args.code, broker_id=args.broker,
            assignee=args.assignee, unassigned=args.unassigned), indent=2))
    elif args.command == "quality":
        print(json.dumps(proposal_quality(store), indent=2))
    elif args.command == "cancel":
        from .store import dump

        try:
            recorded = record_cancellation(
                store, args.policy_number, date.fromisoformat(args.effective),
                args.actor, date.fromisoformat(args.on) if args.on else date.today(),
                reason=args.reason,
            )
        except CancellationError as exc:
            parser.exit(1, f"cancellation refused: {exc}\n")
        print(json.dumps(json.loads(dump(recorded)), indent=2))
    elif args.command == "payables":
        print(json.dumps(return_premiums_view(store), indent=2))
    elif args.command in ("payout-assemble", "payout-approve", "payout-reject", "payout-file"):
        from .store import dump

        try:
            if args.command == "payout-assemble":
                pack = assemble_payout_pack(
                    store, date.fromisoformat(args.on),
                    [p.strip() for p in args.payables.split(",") if p.strip()], args.actor)
                print(json.dumps(json.loads(dump(pack)), indent=2))
            elif args.command == "payout-approve":
                pack = approve_payout_pack(store, args.pack_id, args.approver, policy,
                                           second_approver=args.second)
                print(json.dumps(json.loads(dump(pack)), indent=2))
            elif args.command == "payout-reject":
                pack = reject_payout_pack(store, args.pack_id, args.actor, args.reason)
                print(json.dumps(json.loads(dump(pack)), indent=2))
            else:
                pack = store.payout_pack(args.pack_id)
                if pack is None:
                    parser.exit(1, f"no such payout pack: {args.pack_id}\n")
                print(payout_file(pack), end="")
        except GateError as exc:
            parser.exit(1, f"payout refused: {exc}\n")
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
