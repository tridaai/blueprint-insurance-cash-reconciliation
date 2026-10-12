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
from .config import Policy as PolicyConfig
from .dataset import build_dataset
from .gate import GateError
from .llm import make_reader
from .logging_setup import configure_logging, new_request_id
from .objects import ObjectStore, make_object_store
from .pipeline import ingest_dataset, run_day
from .store import Store, dump, make_store

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

    @app.get("/api/exceptions")
    def exceptions(status: str | None = None):
        s = get_service()
        return [present(e) for e in s.store.exceptions(status=status)]

    @app.get("/api/suspense")
    def suspense(as_of: date | None = None):
        s = get_service()
        return report_mod.suspense_aging(s.store, as_of or date.today())

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

    @app.post("/api/packs/{pack_id}/write-offs")
    def write_off(pack_id: str, body: WriteOffRequest):
        s = get_service()
        try:
            return present(gate.add_write_off(s.store, pack_id, body.exception_id, body.actor))
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc

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
