"""Non-premium bank lines: classified in code, disposed at the gate.

Not every credit on the statement is premium. The bank rebates a
fee, the trust account earns interest, the carrier moves its own
money between its own accounts. Those lines are not a matching
problem — there is no policy they could settle — and leaving them
to the tiers drops them into UNIDENTIFIED suspense, where they age
alongside genuinely unexplained premium cash and blur the one
number auditors watch first.

So classification happens before the tiers, in code, from the
config table in `config.py` (`NON_PREMIUM_RULES`). The rules are
exact: every field a rule names must match, the payer comparison
is against the whole normalised payer name, and there is no fuzzy
path. A line that matches nothing — including a line from the bank
whose narrative does not name a rule's text, and a payer that
merely resembles the carrier — is not classified at all and flows
through the tiers exactly as before. The model is never asked:
classification is a config lookup, not a judgment.

A classified line becomes an entry on a disposition pack
(`NPACK-<date>`), a PostingPack like any other, stopped at the
same human gate: assembled by a named person, approved by a
different one (self-approval refused by the gate's own rule),
rejected with a reason. Approval posts the line to the
non-premium ledger view — a read model over the packs — and
nothing else happens: the line never touches a receivable, never
enters suspense, and never appears in suspense aging. A rejected
disposition leaves the line on the ledger as rejected; the cash
team re-presents it or books it by hand outside the system, but
it still never enters premium suspense, because it was never
premium.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from . import webhooks
from .config import NON_PREMIUM_RULES
from .models import AuditEvent, BankLine, NonPremiumEntry, PostingEntry, PostingPack
from .money import ZERO, as_str
from .store import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def classify_line(line: BankLine, rules=NON_PREMIUM_RULES) -> str | None:
    """The non-premium sub-kind for a bank line, or None.

    A rule matches only when every field it names matches. The
    first matching rule in the table wins; the table is ordered so
    the carrier's own transfers are recognised before the bank's
    fee and interest lines.
    """
    payer = _norm(line.payer_name)
    narrative = (line.narrative or "").lower()
    reference = line.reference or ""
    for rule in rules:
        if "payer_exact" in rule and payer != _norm(rule["payer_exact"]):
            continue
        if "narrative_contains" in rule and rule["narrative_contains"] not in narrative:
            continue
        if "reference_prefix" in rule and not reference.startswith(rule["reference_prefix"]):
            continue
        return rule["kind"]
    return None


def is_disposition_pack(pack: PostingPack) -> bool:
    return pack.pack_id.startswith("NPACK-")


def assemble_disposition_pack(
    store: Store, day: date, classified: list[tuple[BankLine, str]], actor: str
) -> PostingPack | None:
    """Build the day's non-premium disposition pack from classified
    (line, sub-kind) pairs, with the re-match pack's discipline: a
    still-proposed pack for the day gains the new entries; a
    decided one is history, so a suffixed pack is opened instead.
    Returns None when there is nothing to dispose."""
    if not classified:
        return None
    pack = PostingPack(
        pack_id=f"NPACK-{day.isoformat()}", value_date=day, assembled_by=actor
    )
    for line, kind in classified:
        pack.entries.append(PostingEntry(
            kind="non_premium",
            non_premium=NonPremiumEntry(
                bank_line_id=line.bank_line_id, kind=kind, amount=line.amount,
                payer_name=line.payer_name, narrative=line.narrative,
            ),
        ))
    existing = store.pack(pack.pack_id)
    if existing is not None and existing.status.value == "proposed":
        have = {
            e.non_premium.bank_line_id for e in existing.entries if e.non_premium
        }
        for entry in pack.entries:
            if entry.non_premium.bank_line_id not in have:
                existing.entries.append(entry)
        pack = existing
    elif existing is not None:
        n = 2
        while store.pack(f"{pack.pack_id}-{n}") is not None:
            n += 1
        pack.pack_id = f"{pack.pack_id}-{n}"
    store.save_pack(pack)
    store.audit(AuditEvent(
        at=_now(), actor=actor, action="non_premium_pack_assembled",
        subject=pack.pack_id,
        detail=f"{len(pack.entries)} non-premium disposition(s), total "
               f"{pack.non_premium_total}; stops at the gate",
    ))
    webhooks.emit(store, "pack_assembled", pack.pack_id, actor, {
        "non_premium": as_str(pack.non_premium_total),
        "entries": str(len(pack.entries)),
    }, pack_kind="non_premium")
    return pack


def disposition_for_line(store: Store, bank_line_id: str) -> dict | None:
    """The disposition standing for one bank line, if any pack
    carries it: the sub-kind, the pack, and where the gate left it."""
    for pack in sorted(store.packs(), key=lambda p: (p.value_date, p.pack_id)):
        for entry in pack.entries:
            if entry.non_premium and entry.non_premium.bank_line_id == bank_line_id:
                return {
                    "pack_id": pack.pack_id,
                    "kind": entry.non_premium.kind,
                    "amount": as_str(entry.non_premium.amount),
                    "status": pack.status.value,
                    "assembled_by": pack.assembled_by,
                    "approvals": list(pack.approvals),
                }
    return None


def non_premium_ledger(
    store: Store, month: tuple[int, int] | None = None
) -> dict:
    """The non-premium ledger view: every disposition on the books,
    grouped by sub-kind, with the gate's state beside the money.

    Totals are given two ways, because they answer different
    questions: `posted_total` counts approved packs only — the
    money the ledger has actually taken — while the per-kind rows
    carry proposed and rejected totals alongside, so a disposition
    still waiting at the gate is visible rather than silently
    inside or outside the number. With `month`, only packs whose
    value date falls in it count — the form the close pack carries.
    """
    by_kind: dict[str, dict] = {}
    items: list[dict] = []
    posted_total = ZERO
    for pack in sorted(store.packs(), key=lambda p: (p.value_date, p.pack_id)):
        if not is_disposition_pack(pack):
            continue
        if month is not None and (pack.value_date.year, pack.value_date.month) != month:
            continue
        status = pack.status.value
        for entry in pack.entries:
            if not entry.non_premium:
                continue
            np = entry.non_premium
            slot = by_kind.setdefault(np.kind, {
                "count": 0, "approved": ZERO, "proposed": ZERO, "rejected": ZERO,
            })
            slot["count"] += 1
            slot[status] = slot.get(status, ZERO) + np.amount
            if status == "approved":
                posted_total += np.amount
            items.append({
                "bank_line_id": np.bank_line_id,
                "kind": np.kind,
                "amount": as_str(np.amount),
                "payer_name": np.payer_name,
                "pack_id": pack.pack_id,
                "status": status,
                "value_date": pack.value_date.isoformat(),
            })
    return {
        "posted_total": as_str(posted_total),
        "by_kind": {
            kind: {
                "count": slot["count"],
                "approved": as_str(slot["approved"]),
                "proposed": as_str(slot["proposed"]),
                "rejected": as_str(slot["rejected"]),
            }
            for kind, slot in sorted(by_kind.items())
        },
        "items": items,
    }
