"""FastAPI surface: health, the day run, the gate, the reports.

The API is a thin shell over the same services the CLI and the evals
use. It holds no matching or gate logic of its own. Amounts cross the
wire as decimal strings, never floats.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import gate, report as report_mod
from .close_pack import close_pack_export, parse_month, render_close_pack_text
from .config import Policy as PolicyConfig
from .dataset import build_dataset
from .gate import GateError
from .llm import make_reader
from .logging_setup import configure_logging, new_request_id
from .objects import ObjectStore, make_object_store
from .pipeline import ingest_dataset, run_day
from .rematch import rematch_suspense
from .returns import ReturnError, line_history, record_return
from .statement import StatementImportError, import_statement
from .store import Store, dump, make_store
from .workbench import WorkbenchError, add_handling_note, assign_exception, workbench_view

log = logging.getLogger("cash_recon.api")
STATIC = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    actor: str = "cash-team"


class ApproveRequest(BaseModel):
    approver: str
    second_approver: str | None = None


class RejectRequest(BaseModel):
    actor: str
    reason: str


class WriteOffRequest(BaseModel):
    exception_id: str
    actor: str


class RematchRequest(BaseModel):
    actor: str = "cash-team"
    as_of: date | None = None


class StatementImportRequest(BaseModel):
    text: str
    source_name: str = "statement"


class EntryAdjustment(BaseModel):
    entry_index: int
    new_amount: str | None = None
    new_policy: str | None = None
    new_installment: str | None = None


class EditPackRequest(BaseModel):
    actor: str
    adjustments: list[EntryAdjustment]


class AssignRequest(BaseModel):
    assignee: str
    actor: str


class NoteRequest(BaseModel):
    actor: str
    text: str


class ReturnRequest(BaseModel):
    reason: str  # NSF | ACCOUNT_CLOSED | STOP_PAYMENT | RECALL
    actor: str
    on: date | None = None
    amount: str | None = None  # anything but the full line amount is refused, in words


def present(model) -> dict:
    return json.loads(dump(model))


class Service:
    def __init__(self, store: Store, objects: ObjectStore, reader, policy: PolicyConfig) -> None:
        self.store = store
        self.objects = objects
        self.reader = reader
        self.policy = policy


def create_app(service: Service | None = None) -> FastAPI:
    configure_logging()
    svc = service

    def get_service() -> Service:
        nonlocal svc
        if svc is None:
            svc = Service(make_store(), make_object_store(), make_reader(), PolicyConfig.from_env())
        return svc

    app = FastAPI(title="Insurance Cash Reconciliation Agent — Trida AI Blueprint")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = new_request_id()
        if os.environ.get("API_KEY") and request.url.path.startswith("/api"):
            if request.headers.get("x-api-key") != os.environ["API_KEY"]:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness():
        s = get_service()
        try:
            s.store.brokers()
            store_ok = True
        except Exception:
            store_ok = False
        return {
            "ready": store_ok,
            "store": type(s.store).__name__,
            "profile": getattr(s.store, "profile_name", "unknown"),
            "database": str(getattr(s.store, "path", "")) or None,
            "objects": type(s.objects).__name__,
            "reader": s.reader.name,
        }

    @app.get("/", response_class=HTMLResponse)
    def console():
        return (STATIC / "index.html").read_text()

    @app.post("/api/seed")
    def seed():
        s = get_service()
        ds = build_dataset()
        ingest_dataset(s.store, s.objects, ds)
        return {
            "brokers": len(ds.brokers),
            "policies": len(ds.receivables),
            "bank_lines": len(ds.bank_lines),
            "remittances": len(ds.remittances),
        }

    @app.post("/api/days/{day}/run")
    def run(day: date, body: RunRequest):
        s = get_service()
        result = run_day(s.store, s.objects, s.reader, s.policy, day, actor=body.actor)
        return {
            "day": result["day"],
            "pack_id": result["pack_id"],
            "document_notes": result["document_notes"],
            "report": result["report"],
        }

    @app.get("/api/days/{day}/report")
    def day_report(day: date):
        s = get_service()
        return report_mod.day_report(s.store, day, s.policy)

    @app.post("/api/statements/import")
    def import_stmt(body: StatementImportRequest):
        s = get_service()
        try:
            return import_statement(s.store, body.text, body.source_name)
        except StatementImportError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/exceptions")
    def exceptions(status: str | None = None):
        s = get_service()
        return [present(e) for e in s.store.exceptions(status=status)]

    @app.get("/api/workbench")
    def workbench(as_of: date | None = None, code: str | None = None,
                  broker_id: str | None = None, assignee: str | None = None,
                  unassigned: bool = False):
        s = get_service()
        return workbench_view(
            s.store, as_of or date.today(), code=code, broker_id=broker_id,
            assignee=assignee, unassigned=unassigned,
        )

    @app.post("/api/exceptions/{exception_id}/assign")
    def assign(exception_id: str, body: AssignRequest):
        s = get_service()
        try:
            return present(assign_exception(s.store, exception_id, body.assignee, body.actor))
        except WorkbenchError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/exceptions/{exception_id}/notes")
    def add_note(exception_id: str, body: NoteRequest):
        s = get_service()
        try:
            return present(add_handling_note(s.store, exception_id, body.actor, body.text))
        except WorkbenchError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/suspense")
    def suspense(as_of: date | None = None):
        s = get_service()
        return report_mod.suspense_aging(s.store, as_of or date.today())

    @app.get("/api/brokers/scorecard")
    def scorecard(as_of: date | None = None):
        s = get_service()
        return report_mod.broker_scorecard(s.store, as_of or date.today())

    @app.get("/api/close/{month}")
    def close(month: str):
        s = get_service()
        try:
            year, mon = parse_month(month)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return close_pack_export(s.store, year, mon)

    @app.get("/api/close/{month}/text")
    def close_text(month: str):
        s = get_service()
        try:
            year, mon = parse_month(month)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return PlainTextResponse(
            render_close_pack_text(close_pack_export(s.store, year, mon)),
            media_type="text/plain",
        )

    @app.post("/api/suspense/rematch")
    def rematch(body: RematchRequest):
        s = get_service()
        return rematch_suspense(
            s.store, s.reader, s.policy, body.actor, body.as_of or date.today()
        )

    @app.get("/api/packs")
    def packs():
        s = get_service()
        return [present(p) for p in s.store.packs()]

    @app.get("/api/packs/{pack_id}")
    def pack(pack_id: str):
        s = get_service()
        found = s.store.pack(pack_id)
        if found is None:
            raise HTTPException(404, f"no such pack: {pack_id}")
        return present(found)

    @app.post("/api/packs/{pack_id}/approve")
    def approve(pack_id: str, body: ApproveRequest):
        s = get_service()
        try:
            return present(gate.approve_pack(s.store, pack_id, body.approver, s.policy, body.second_approver))
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/packs/{pack_id}/reject")
    def reject(pack_id: str, body: RejectRequest):
        s = get_service()
        try:
            return present(gate.reject_pack(s.store, pack_id, body.actor, body.reason))
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/packs/{pack_id}/edit")
    def edit_pack(pack_id: str, body: EditPackRequest):
        s = get_service()
        from decimal import Decimal, InvalidOperation

        adjustments = []
        for adj in body.adjustments:
            item = {"entry_index": adj.entry_index, "new_policy": adj.new_policy,
                    "new_installment": adj.new_installment}
            if adj.new_amount is not None:
                try:
                    item["new_amount"] = Decimal(adj.new_amount)
                except InvalidOperation:
                    raise HTTPException(422, f"new_amount is not a decimal: {adj.new_amount!r}")
            adjustments.append(item)
        try:
            return present(gate.edit_pack_entries(s.store, pack_id, body.actor, adjustments))
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/proposals/quality")
    def proposals_quality():
        s = get_service()
        return report_mod.proposal_quality(s.store)

    @app.post("/api/packs/{pack_id}/write-offs")
    def write_off(pack_id: str, body: WriteOffRequest):
        s = get_service()
        try:
            return present(gate.add_write_off(s.store, pack_id, body.exception_id, body.actor))
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/returns")
    def returns(on: date | None = None):
        s = get_service()
        return [present(r) for r in s.store.returns(on=on)]

    @app.post("/api/bank-lines/{bank_line_id}/returns")
    def record_line_return(bank_line_id: str, body: ReturnRequest):
        s = get_service()
        from decimal import Decimal, InvalidOperation

        amount = None
        if body.amount is not None:
            try:
                amount = Decimal(body.amount)
            except InvalidOperation:
                raise HTTPException(422, f"amount is not a decimal: {body.amount!r}")
        try:
            ret = record_return(
                s.store, bank_line_id, body.reason, body.actor,
                body.on or date.today(), amount=amount,
            )
        except ReturnError as exc:
            raise HTTPException(409, str(exc)) from exc
        return present(ret)

    @app.get("/api/bank-lines/{bank_line_id}/history")
    def bank_line_history(bank_line_id: str):
        s = get_service()
        history = line_history(s.store, bank_line_id)
        if history["bank_line"] is None:
            raise HTTPException(404, f"no such bank line: {bank_line_id}")
        return history

    @app.get("/api/packs/{pack_id}/posting-file")
    def posting_file(pack_id: str):
        s = get_service()
        found = s.store.pack(pack_id)
        if found is None:
            raise HTTPException(404, f"no such pack: {pack_id}")
        try:
            return PlainTextResponse(gate.posting_file(found), media_type="text/csv")
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/audit")
    def audit(subject: str | None = None):
        s = get_service()
        return [present(e) for e in s.store.audit_events(subject)]

    @app.get("/api/receivables")
    def receivables():
        s = get_service()
        return [present(r) for r in s.store.receivables()]

    return app


app = create_app()
