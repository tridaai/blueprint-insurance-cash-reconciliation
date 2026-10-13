"""Gate webhooks: telling the rest of the operation when money
moves through the gate.

When a pack is assembled, approved, or rejected, or a return is
recorded, a small JSON event is POSTed to the URL in the
`CASH_RECON_WEBHOOK_URL` environment variable — the event kind,
the pack or bank line id, the actor, and the totals, nothing
else (no internal paths, no document text). Unset means
disabled, and `/readiness` says so.

Delivery is fire-and-forget from the caller's perspective: a
bounded-timeout POST on the standard library's HTTP client, one
retry, then it stops. A delivery that still fails is written to
the audit trail as `webhook_failed` with the error class — the
failure is visible in the books, but it never blocks, delays
beyond the timeout, or fails the gate act itself. The gate
decides on the evidence in front of the controller, not on
whether a downstream system is awake.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from .models import AuditEvent
from .store import Store

ENV_VAR = "CASH_RECON_WEBHOOK_URL"
TIMEOUT_SECONDS = 2.0
ATTEMPTS = 2  # the first try and one retry; then it stops


def webhook_url() -> str | None:
    return os.environ.get(ENV_VAR) or None


def webhook_status() -> str:
    """For /readiness: is a receiver configured at all?"""
    return "configured" if webhook_url() else "disabled"


def emit(
    store: Store,
    kind: str,
    subject_id: str,
    actor: str,
    totals: dict,
    pack_kind: str | None = None,
) -> None:
    """POST one gate event, best-effort. Never raises: a failed
    delivery lands in the audit trail as `webhook_failed` and the
    caller's act stands."""
    url = webhook_url()
    if not url:
        return
    event: dict = {
        "kind": kind,
        "id": subject_id,
        "actor": actor,
        "totals": totals,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if pack_kind:
        event["pack_kind"] = pack_kind
    body = json.dumps(event).encode()
    error: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            request = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
            error = None
            break
        except Exception as exc:  # delivery must never reach the gate act
            error = exc
    if error is not None:
        store.audit(AuditEvent(
            at=datetime.now(timezone.utc), actor="webhook",
            action="webhook_failed", subject=subject_id,
            detail=(
                f"{kind} delivery to the configured webhook failed after "
                f"{ATTEMPTS} attempts: {type(error).__name__}"
            ),
        ))
