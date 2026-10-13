"""Production smoke: prove the production profile against real services.

    DATABASE_URL=postgresql://… S3_BUCKET=… S3_ENDPOINT_URL=… \
    S3_ACCESS_KEY_ID=… S3_SECRET_ACCESS_KEY=… make prod-smoke

This is the manual production verification (PostgreSQL + pgvector
and an S3-compatible store, stood up natively or via compose) made
repeatable. It exists to prove the PRODUCTION profile only: it
refuses to run when DATABASE_URL or the S3 variables are unset,
rather than quietly passing against the SQLite local profile or an
in-memory object store.

What it does, in order, failing loudly with the step name on any
mismatch:

  profile            the env names the production profile (and the
                       localhost service endpoints are in NO_PROXY —
                       the script extends it for its own processes
                       and says so, because behind an HTTP proxy a
                       localhost endpoint otherwise routes to the
                       proxy and never reaches the service)
  api-readiness      the real API is booted (uvicorn subprocess)
                       and its /readiness must report ready, the
                       postgres profile and the S3 object store
  seed               the synthetic September dataset is ingested
                       through the production stores — into a FRESH
                       database: the step refuses if the store
                       already holds bank lines, so the smoke never
                       tramples an existing book
  run-day            one September day (2026-09-02) runs through the
                       real pipeline; its report must foot
  approve            the day's pack is approved at the gate by a
                       named controller, and stands approved
  close-pack-digest  the September close pack is assembled twice;
                       the two SHA-256 digests must be identical
  document-round-trip  one remittance document is read back from
                       the object store and must be byte-identical
                       to the text the pipeline ingested

The data steps call the same store/pipeline/gate functions the API
calls (the demo's pattern); only the readiness step boots the API,
because "the service comes up against the real stores" is itself
part of the proof. The script deletes nothing: what it creates —
the seeded books in the target database, the LangGraph checkpoint
tables, the documents in the bucket — stays for inspection. Run it
against a throwaway database (the conformance battery's convention),
not one you keep.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

API_PORT = 8417
DAY = date(2026, 9, 2)
ASSEMBLER = "cash-team"
CONTROLLER = "Priya Nair"


class SmokeFailure(RuntimeError):
    """A step failed. The message is the detail; the step name is
    attached by the runner."""


def _ensure_no_proxy(*hosts: str) -> list[str]:
    """Make sure the localhost service endpoints bypass any HTTP
    proxy in this process and the API child. Returns what was added."""
    added: list[str] = []
    for var in ("NO_PROXY", "no_proxy"):
        current = [h.strip() for h in os.environ.get(var, "").split(",") if h.strip()]
        for host in hosts:
            if host and host not in current:
                current.append(host)
                if host not in added:
                    added.append(host)
        os.environ[var] = ",".join(current)
    return added


def _http_get(url: str) -> dict:
    # A no-proxy opener: the readiness poll must reach the child on
    # localhost directly, whatever the ambient proxy env says.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as resp:
        return json.loads(resp.read())


def step_profile() -> str:
    if not os.environ.get("DATABASE_URL"):
        raise SmokeFailure(
            "DATABASE_URL is not set — prod-smoke proves the production "
            "profile only and will not run against the SQLite local profile"
        )
    missing = [v for v in ("S3_BUCKET", "S3_ENDPOINT_URL") if not os.environ.get(v)]
    if missing:
        raise SmokeFailure(
            f"{', '.join(missing)} not set — without them the object store "
            "falls back to an in-memory double and there is nothing to prove"
        )
    endpoint = os.environ["S3_ENDPOINT_URL"]
    host = endpoint.split("://", 1)[-1].split(":")[0].split("/")[0]
    added = _ensure_no_proxy("localhost", "127.0.0.1", host)
    note = f"; NO_PROXY extended with {', '.join(added)} for this run" if added else ""
    return f"postgres profile, bucket {os.environ['S3_BUCKET']} at {endpoint}{note}"


def step_api_readiness() -> str:
    from cash_recon.store import store_profile

    kind, _ = store_profile()
    if kind != "postgres":
        raise SmokeFailure(f"store profile is {kind!r}, not 'postgres'")
    log_path = Path(tempfile.mkdtemp(prefix="cash-recon-smoke-")) / "api.log"
    env = dict(os.environ)
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "cash_recon.api:app",
             "--host", "127.0.0.1", "--port", str(API_PORT)],
            stdout=log_file, stderr=subprocess.STDOUT, env=env,
        )
        try:
            deadline = time.time() + 60
            readiness = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-2000:]
                    raise SmokeFailure(f"the API exited during boot (code {proc.returncode}):\n{tail}")
                try:
                    readiness = _http_get(f"http://127.0.0.1:{API_PORT}/readiness")
                    break
                except Exception:
                    time.sleep(0.5)
            if readiness is None:
                tail = log_path.read_text(errors="replace")[-2000:]
                raise SmokeFailure(f"the API did not answer /readiness within 60s:\n{tail}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    if readiness.get("ready") is not True:
        raise SmokeFailure(f"/readiness reports not ready: {readiness}")
    if readiness.get("profile") != "postgres":
        raise SmokeFailure(f"/readiness profile is {readiness.get('profile')!r}, not 'postgres'")
    if readiness.get("objects") != "S3ObjectStore":
        raise SmokeFailure(f"/readiness objects is {readiness.get('objects')!r}, not 'S3ObjectStore'")
    return (f"API booted on :{API_PORT}; /readiness ready, profile postgres, "
            f"objects S3ObjectStore, reader {readiness.get('reader')}")


def _stores():
    from cash_recon.objects import S3ObjectStore, make_object_store
    from cash_recon.store import PostgresStore, make_store

    store = make_store()
    if not isinstance(store, PostgresStore):
        raise SmokeFailure(f"store is {type(store).__name__}, not PostgresStore")
    objects = make_object_store()
    if not isinstance(objects, S3ObjectStore):
        raise SmokeFailure(f"object store is {type(objects).__name__}, not S3ObjectStore")
    return store, objects


def step_seed(ctx: dict) -> str:
    from cash_recon.dataset import build_dataset
    from cash_recon.pipeline import ingest_dataset

    store, objects = _stores()
    ctx["store"], ctx["objects"] = store, objects
    existing = store.bank_lines()
    if existing:
        raise SmokeFailure(
            f"the store already holds {len(existing)} bank lines — prod-smoke seeds "
            "the synthetic month and will not trample an existing book; point "
            "DATABASE_URL at a fresh database"
        )
    ds = build_dataset()
    ctx["dataset"] = ds
    ingest_dataset(store, objects, ds)
    counts = (len(store.brokers()), len(store.receivables()),
              len(store.bank_lines()), len(store.remittances()))
    expected = (len(ds.brokers), len(ds.receivables), len(ds.bank_lines), len(ds.remittances))
    if counts != expected:
        raise SmokeFailure(f"seeded counts {counts} != dataset {expected}")
    return (f"seeded {expected[1]} policies, {expected[2]} bank lines, "
            f"{expected[3]} remittances through Postgres + S3")


def step_run_day(ctx: dict) -> str:
    from cash_recon.config import Policy as PolicyConfig
    from cash_recon.llm import OfflineReader
    from cash_recon.pipeline import run_day

    policy = PolicyConfig.from_env()
    ctx["policy"] = policy
    result = run_day(ctx["store"], ctx["objects"], OfflineReader(), policy,
                     DAY, actor=ASSEMBLER)
    ctx["pack_id"] = result["pack_id"]
    if not result["pack_id"]:
        raise SmokeFailure(f"the {DAY} run assembled no pack")
    if result["report"]["footing_ok"] is not True:
        raise SmokeFailure(f"the {DAY} report does not foot: {result['report']['footing_note']}")
    rep = result["report"]
    return (f"{DAY} ran; pack {result['pack_id']}, bank {rep['bank_total']}, "
            f"allocated {rep['allocated_total']}, footing OK")


def step_approve(ctx: dict) -> str:
    from cash_recon.gate import approve_pack

    approve_pack(ctx["store"], ctx["pack_id"], CONTROLLER, ctx["policy"])
    pack = ctx["store"].pack(ctx["pack_id"])
    if pack is None or pack.status.value != "approved":
        raise SmokeFailure(f"pack {ctx['pack_id']} does not stand approved after approval")
    return f"pack {ctx['pack_id']} approved by {CONTROLLER} and stands approved"


def step_close_pack_digest(ctx: dict) -> str:
    from cash_recon.close_pack import close_pack_export

    first = close_pack_export(ctx["store"], 2026, 9)
    second = close_pack_export(ctx["store"], 2026, 9)
    if not first["sha256"] or first["sha256"] != second["sha256"]:
        raise SmokeFailure(
            f"close-pack digests differ across regeneration: "
            f"{first['sha256'][:16]}… vs {second['sha256'][:16]}…"
        )
    footed = first["pack"]["control_roll"]["footed"]
    return (f"September close pack assembled twice, SHA-256 identical "
            f"({first['sha256'][:16]}…); control roll footed: {footed}")


def step_document_round_trip(ctx: dict) -> str:
    rem = ctx["dataset"].remittances[0]
    key = f"remittances/{rem.remittance_id}.txt"
    back = ctx["objects"].get(key)
    expected = rem.text.encode()
    if back != expected:
        raise SmokeFailure(
            f"{key} read back {len(back)} bytes, ingested {len(expected)} — not identical"
        )
    digest = hashlib.sha256(back).hexdigest()
    return f"{key} round-tripped byte-identical ({len(back)} bytes, SHA-256 {digest[:16]}…)"


STEPS = [
    ("profile", step_profile, False),
    ("api-readiness", step_api_readiness, False),
    ("seed", step_seed, True),
    ("run-day", step_run_day, True),
    ("approve", step_approve, True),
    ("close-pack-digest", step_close_pack_digest, True),
    ("document-round-trip", step_document_round_trip, True),
]


def main() -> int:
    ctx: dict = {}
    for name, fn, takes_ctx in STEPS:
        try:
            detail = fn(ctx) if takes_ctx else fn()
        except SmokeFailure as exc:
            print(f"PROD SMOKE: FAIL at step {name}: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # a loud failure, never a quiet pass
            print(f"PROD SMOKE: FAIL at step {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"[prod-smoke] {name}: ok — {detail}")
    print(f"PROD SMOKE: PASS — {len(STEPS)} steps against the production profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
