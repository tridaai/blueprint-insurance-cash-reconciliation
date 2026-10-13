"""Broker statement reconciliation: the broker's version of the
month, checked against the books.

Brokers remit net and keep their own books. Once a month each
broker sends a statement of what it believes it remitted: per
policy, the gross it billed, the commission it says it took, and
the net it says it sent. Until now the desk could only check that
statement against the books by eye. This module is the check.

The import carries the bank statement import's discipline: the
file is parsed whole, its trailer controls (a CONTROL TOTAL over
the lines' nets, a CONTROL COUNT of the data rows) must match
exactly, and any failure — a bad row, a tampered total, an
unknown broker — refuses the whole import with nothing stored.
A statement's id is a digest of its text, so re-importing the
same file dedupes; a corrected statement for the same broker and
period supersedes the standing one, which stays on the record
marked with what replaced it. Importing a statement changes no
money: its figures are claims, not postings.

The reconciliation is a read model over the standing statement
and the books. The books side is cash actually APPLIED — the
allocations of approved posting packs, attributed to the broker
the same way the day reports attribute them, with each
allocation's gross (billed) delta replayed through the gate's own
arithmetic at the rate the gate itself used (a combination leg is
priced at its own line's date, an ordinary allocation at its
pack's value date — the same rule approval applied). From those,
per policy and currency:

  AGREED              the statement's net is the cash the books
                      applied, and its gross and commission agree
                      with the contracted figures, to the cent.
  COMMISSION_DIFFERS  the gross agrees; the commission the broker
                      says it took is not the contracted rate in
                      force (rate history prices the expectation).
  AMOUNT_DIFFERS      the policy matches by period, but the gross
                      or the net does not — both figures shown.
  MISSING_ON_BOOKS    the broker says it paid; no cash from this
                      broker was applied to the policy in the
                      period.
  MISSING_ON_STATEMENT the books applied this broker's cash to
                      the policy in the period; the statement
                      never mentions it.

The boundary, stated plainly: this report reconciles APPLIED
cash. Cash the broker sent that still stands unapplied in
suspense is not a disagreement with the broker — it is the desk's
own open work, and it is reconciled where it lives: the suspense
aging and the close pack. A policy whose only October cash sits in
suspense under an open exception raises no row here.

Totals foot, per currency: the statement's net total is the sum
of its statement-side classes; the books total is the sum of its
books-side classes; and the books' posted cash, plus what stands
proposed at the gate, plus what stands on rejected packs, equals
the month's by-broker day splits — the same index the daily
reports read. Where an identity cannot foot, the report says NOT
FOOTED and names the difference instead of smoothing it over.

Differences are work, not edits. A row that maps to an exception
already open on the books names it (with its assignee and note
count), and assignment and notes happen through the existing
workbench — this module adds no handling machinery of its own. A
MISSING_ON_BOOKS row opens nothing: the broker's claim is
reported and exportable, and that is all. The export is digested
(SHA-256 over a wall-clock-free body); regenerated from unchanged
books it is byte-identical, like the close pack and the line
export it keeps company with.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from . import gate as _gate
from .config import Policy as PolicyConfig
from .models import (
    AuditEvent,
    BrokerStatement,
    BrokerStatementLine,
)
from .money import ZERO, as_str, cents, commission_on
from .report import _broker_key, day_report
from .store import Store

CLASSES = (
    "AGREED",
    "AMOUNT_DIFFERS",
    "COMMISSION_DIFFERS",
    "MISSING_ON_BOOKS",
    "MISSING_ON_STATEMENT",
)

BOUNDARY_NOTE = (
    "This report reconciles applied cash only. Cash a broker sent "
    "that still stands unapplied in suspense is the desk's open "
    "work, not a disagreement with the broker: it is reconciled in "
    "the suspense aging and the month-end close pack, and it "
    "raises no row here."
)


class BrokerStatementError(ValueError):
    """A broker statement import or reconciliation was refused,
    in words. Nothing is stored on an import refusal."""


# ---------------------------------------------------------------------
# Import — the bank statement import's discipline, a broker's columns
# ---------------------------------------------------------------------

def _norm(cell: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (cell or "").lower()).strip()


_POLICY_ALIASES = {"policy", "policy number", "policy reference", "policy no", "policy ref"}
_GROSS_ALIASES = {"gross", "gross billed", "gross premium", "billed", "premium"}
_COMMISSION_ALIASES = {"commission", "commission taken", "broker commission", "comm"}
_NET_ALIASES = {"net", "net remitted", "net remitted amount", "remitted", "net amount"}
_REFERENCE_ALIASES = {"reference", "remittance reference", "broker reference", "ref"}
_CURRENCY_ALIASES = {"currency", "ccy"}
_BROKER_KEYS = {"broker", "broker id", "broker code"}
_PERIOD_KEYS = {"period", "month", "statement period"}
_TRAILER_RE = re.compile(r"^\s*(control|grand)\s+(total|count)\b", re.I)


def _money(raw: str, what: str) -> Decimal:
    try:
        return cents(Decimal((raw or "").replace(",", "").strip()))
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise BrokerStatementError(f"{what}: {raw!r} is not an amount") from exc


def _period_parts(period: str) -> tuple[int, int]:
    parts = (period or "").split("-")
    if len(parts) != 2:
        raise BrokerStatementError(f"period must be YYYY-MM, got {period!r}")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise BrokerStatementError(f"period must be YYYY-MM, got {period!r}") from exc
    if not 1 <= month <= 12:
        raise BrokerStatementError(f"period must be YYYY-MM, got {period!r}")
    return year, month


def import_broker_statement(store: Store, text: str, source_name: str = "") -> dict:
    """Parse, control-check and store one broker statement.

    All-or-nothing, like the bank statement import: any refusal
    leaves the store exactly as it was. A file whose digest is
    already on the books dedupes — importing it again changes
    nothing. A different statement for a broker and period that
    already has a standing statement supersedes it: the old record
    is marked, never deleted, and the reconciliation reads the
    standing one.
    """
    rows = list(csv.reader(io.StringIO(text)))
    broker_id: str | None = None
    period: str | None = None
    header_at: int | None = None
    columns: dict[str, int] = {}

    for i, row in enumerate(rows):
        if not row or not any((c or "").strip() for c in row):
            continue
        first = _norm(row[0])
        key, _, inline_value = (row[0] or "").partition(":")
        if first not in _BROKER_KEYS and first not in _PERIOD_KEYS \
                and inline_value and _norm(key) in (_BROKER_KEYS | _PERIOD_KEYS):
            # "Broker: BRK-FERN" on one cell, the way a person writes it.
            row = [key.strip(), inline_value.strip()]
            first = _norm(row[0])
        if first in _BROKER_KEYS and len(row) > 1:
            broker_id = (row[1] or "").strip()
            continue
        if first in _PERIOD_KEYS and len(row) > 1:
            period = (row[1] or "").strip()
            continue
        norms = [_norm(c) for c in row]
        if any(n in _POLICY_ALIASES for n in norms) and any(n in _NET_ALIASES for n in norms):
            header_at = i
            for idx, n in enumerate(norms):
                if n in _POLICY_ALIASES:
                    columns.setdefault("policy", idx)
                elif n in _GROSS_ALIASES:
                    columns.setdefault("gross", idx)
                elif n in _COMMISSION_ALIASES:
                    columns.setdefault("commission", idx)
                elif n in _NET_ALIASES:
                    columns.setdefault("net", idx)
                elif n in _REFERENCE_ALIASES:
                    columns.setdefault("reference", idx)
                elif n in _CURRENCY_ALIASES:
                    columns.setdefault("currency", idx)
            break

    if header_at is None:
        raise BrokerStatementError(
            "no statement header found — expected a row naming the policy "
            "and net columns (policy, gross billed, commission, net remitted, "
            "remittance reference)"
        )
    for needed in ("policy", "gross", "commission", "net"):
        if needed not in columns:
            raise BrokerStatementError(f"the statement header has no {needed} column")
    if not broker_id:
        raise BrokerStatementError(
            "the statement names no broker — expected a 'Broker: <broker id>' line"
        )
    if not period:
        raise BrokerStatementError(
            "the statement names no period — expected a 'Period: YYYY-MM' line"
        )
    _period_parts(period)
    broker = next((b for b in store.brokers() if b.broker_id == broker_id), None)
    if broker is None:
        raise BrokerStatementError(
            f"broker {broker_id} is not on the books — a statement is only "
            "imported for a broker the desk has a contracted record for"
        )

    def cell(row: list[str], key: str) -> str:
        idx = columns.get(key)
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    lines: list[BrokerStatementLine] = []
    control_total: Decimal | None = None
    control_count: int | None = None
    for row in rows[header_at + 1:]:
        if not row or not any((c or "").strip() for c in row):
            continue
        first = (row[0] or "").strip()
        if _TRAILER_RE.match(first):
            kind = "total" if "total" in first.lower() else "count"
            value: str | None = None
            net_idx = columns["net"]
            if net_idx < len(row) and (row[net_idx] or "").strip():
                value = row[net_idx]
            else:
                for c in row[1:]:
                    if (c or "").strip():
                        value = c
                        break
            if value is None:
                raise BrokerStatementError(f"trailer row {first!r} carries no value")
            if kind == "total":
                control_total = _money(value, "control total")
            else:
                try:
                    control_count = int(str(value).replace(",", "").strip())
                except ValueError as exc:
                    raise BrokerStatementError(
                        f"control count: {value!r} is not a count") from exc
            continue
        if _norm(row[0]) in _BROKER_KEYS or _norm(row[0]) in _PERIOD_KEYS:
            continue
        policy = cell(row, "policy")
        if not policy:
            raise BrokerStatementError(f"a statement row names no policy: {row!r}")
        gross = _money(cell(row, "gross"), f"gross for {policy}")
        commission = _money(cell(row, "commission"), f"commission for {policy}")
        net = _money(cell(row, "net"), f"net for {policy}")
        for label, amount in (("gross", gross), ("commission", commission), ("net", net)):
            if amount < ZERO:
                raise BrokerStatementError(
                    f"{policy}: a negative {label} ({as_str(amount)}) is not a "
                    "remittance — the statement is refused whole"
                )
        lines.append(BrokerStatementLine(
            policy_number=policy,
            gross=gross,
            commission=commission,
            net=net,
            reference=cell(row, "reference"),
            currency=(cell(row, "currency") or "USD").upper(),
        ))

    if not lines:
        raise BrokerStatementError("the statement carries no data rows")
    if control_total is not None:
        actual = sum((l.net for l in lines), ZERO)
        if control_total != actual:
            raise BrokerStatementError(
                f"control total mismatch: the file states {as_str(control_total)} "
                f"but its {len(lines)} line(s) sum to {as_str(actual)} — "
                "the import is refused whole and nothing was stored"
            )
    if control_count is not None and control_count != len(lines):
        raise BrokerStatementError(
            f"control count mismatch: the file states {control_count} row(s) "
            f"but carries {len(lines)} — the import is refused whole and "
            "nothing was stored"
        )

    statement_id = "BSTMT-" + hashlib.sha256(text.encode()).hexdigest()[:10].upper()
    if store.broker_statement(statement_id) is not None:
        return {
            "statement_id": statement_id,
            "broker_id": broker_id,
            "period": period,
            "lines": len(lines),
            "net_total": as_str(sum((l.net for l in lines), ZERO)),
            "control_total": as_str(control_total) if control_total is not None else None,
            "control_count": control_count,
            "deduped": True,
            "superseded": [],
        }

    superseded: list[str] = []
    for standing in store.broker_statements(broker_id=broker_id, period=period):
        if standing.superseded_by is None:
            standing.superseded_by = statement_id
            store.save_broker_statement(standing)
            superseded.append(standing.statement_id)

    stmt = BrokerStatement(
        statement_id=statement_id,
        broker_id=broker_id,
        period=period,
        source_name=source_name,
        control_total=control_total,
        control_count=control_count,
        lines=lines,
    )
    store.save_broker_statement(stmt)
    store.audit(AuditEvent(
        at=datetime.now(timezone.utc),
        actor="statement-import",
        action="broker_statement_imported",
        subject=statement_id,
        detail=(
            f"{broker.name} statement for {period}: {len(lines)} line(s), net "
            f"{as_str(stmt.net_total)}"
            + (f"; supersedes {', '.join(superseded)}" if superseded else "")
        ),
    ))
    return {
        "statement_id": statement_id,
        "broker_id": broker_id,
        "period": period,
        "lines": len(lines),
        "net_total": as_str(stmt.net_total),
        "control_total": as_str(control_total) if control_total is not None else None,
        "control_count": control_count,
        "deduped": False,
        "superseded": superseded,
    }


def standing_statement(store: Store, broker_id: str, period: str) -> BrokerStatement | None:
    """The statement the reconciliation reads for a broker and
    period: the one nothing has superseded."""
    standing = [
        s for s in store.broker_statements(broker_id=broker_id, period=period)
        if s.superseded_by is None
    ]
    return sorted(standing, key=lambda s: s.statement_id)[0] if standing else None


def statement_summaries(store: Store) -> list[dict]:
    """Every imported statement, standing and superseded alike —
    the import history, for the console and the operator."""
    return [
        {
            "statement_id": s.statement_id,
            "broker_id": s.broker_id,
            "period": s.period,
            "source_name": s.source_name,
            "lines": len(s.lines),
            "net_total": as_str(s.net_total),
            "standing": s.superseded_by is None,
            "superseded_by": s.superseded_by,
        }
        for s in sorted(
            store.broker_statements(),
            key=lambda s: (s.period, s.broker_id, s.statement_id),
        )
    ]


# ---------------------------------------------------------------------
# The books side: applied cash, replayed the way the gate applied it
# ---------------------------------------------------------------------

def _applied_figures(store: Store) -> dict[tuple[str, str, str], dict]:
    """Per approved allocation — keyed by (pack, bank line,
    installment) — the cash applied, the gross (billed) delta it
    settled, the rate the gate priced it at, and the contracted
    commission that rate implies.

    The replay mirrors the gate exactly: pristine receivables,
    approved packs in decision order, entries in pack order, and
    the gate's own rate selection — a combination leg and a
    resolution allocation at the rate in force on the leg's bank
    line date, an ordinary allocation at the rate in force on its
    pack's value date. (The returns replay prices every entry at
    the pack date; its totals survive that because the
    settle-in-full cap corrects them. A commission expectation is
    per allocation and has no such correction, so this replay
    follows the gate's rule entry by entry.)
    """
    brokers = {b.broker_id: b for b in store.brokers()}
    lines = {l.bank_line_id: l for l in store.bank_lines()}
    pristine = {}
    for rec in store.receivables():
        clone = rec.model_copy(deep=True)
        for inst in clone.installments:
            inst.amount_applied = ZERO
            inst.status = "OPEN"
        pristine[clone.policy_number] = clone

    figures: dict[tuple[str, str, str], dict] = {}
    approved = [p for p in store.packs() if p.status.value == "approved"]
    approved.sort(key=lambda p: (p.decided_at or datetime.min.replace(tzinfo=timezone.utc), p.pack_id))
    for pack in approved:
        per_line_rate = (
            pack.pack_id.startswith("PACK-COMB")
            or (pack.resolution is not None and pack.resolution.chosen_candidate is not None)
        )
        for entry in pack.entries:
            alloc = entry.allocation
            if alloc is None:
                continue
            rec = pristine.get(alloc.policy_number)
            if rec is None:
                continue
            broker = brokers.get(rec.broker_id) if rec.broker_id else None
            if broker is None:
                rate = None
            elif per_line_rate:
                line = lines.get(alloc.bank_line_id)
                rate = broker.rate_on(
                    line.value_date if line is not None else pack.value_date)
            else:
                rate = broker.rate_on(pack.value_date)
            inst = next(
                (i for i in rec.installments
                 if i.installment_id == alloc.installment_id),
                None,
            )
            if inst is None:
                continue
            before = inst.amount_applied
            _gate._apply(rec, inst, alloc, rate)
            delta = inst.amount_applied - before
            key = (pack.pack_id, alloc.bank_line_id, alloc.installment_id)
            slot = figures.setdefault(key, {
                "cash": ZERO, "gross": ZERO, "expected_commission": ZERO,
                "rates": set(),
            })
            slot["cash"] += alloc.amount
            slot["gross"] += delta
            if rate is not None:
                slot["expected_commission"] += commission_on(delta, rate)
                slot["rates"].add(str(rate))
    return figures


def _pct(rate: Decimal) -> str:
    return f"{(rate * 100).normalize()}%"


# ---------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------

def build_reconciliation(
    store: Store, broker_id: str, period: str,
    policy: PolicyConfig | None = None,
) -> dict:
    """Classify every line of the broker's standing statement for
    the period against the books, and every book the statement
    omits. Pure read: nothing in the books changes."""
    policy = policy or PolicyConfig.from_env()
    stmt = standing_statement(store, broker_id, period)
    if stmt is None:
        raise BrokerStatementError(
            f"no broker statement on the books for {broker_id} in {period} — "
            "import one first"
        )
    broker = next((b for b in store.brokers() if b.broker_id == broker_id), None)
    if broker is None:
        raise BrokerStatementError(f"broker {broker_id} is not on the books")
    year, month = _period_parts(period)
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])

    lines = {l.bank_line_id: l for l in store.bank_lines()}
    names = {b.broker_id: b.name for b in store.brokers()}
    receivables = {r.policy_number: r for r in store.receivables()}
    figures = _applied_figures(store)

    def attributed(line) -> bool:
        return line is not None and first <= line.value_date <= last \
            and _broker_key(store, line, names) == broker_id

    # The books, grouped per (policy, currency): approved cash only.
    books: dict[tuple[str, str], dict] = {}
    # The gate-state bridge, per currency: what the allocation
    # index holds for this broker in this period, split by the
    # state of the pack that carries it.
    bridge: dict[str, dict[str, Decimal]] = {}

    def bridge_add(ccy: str, term: str, amount: Decimal) -> None:
        slot = bridge.setdefault(ccy, {
            "posted": ZERO, "at_gate": ZERO, "on_rejected": ZERO})
        slot[term] += amount

    for pack in store.packs():
        state = pack.status.value
        if state not in ("approved", "proposed", "rejected"):
            continue
        for entry in pack.entries:
            alloc = entry.allocation
            if alloc is None:
                continue
            line = lines.get(alloc.bank_line_id)
            if not attributed(line):
                continue
            if state == "approved":
                bridge_add(alloc.currency, "posted", alloc.amount)
                fig = figures.get((pack.pack_id, alloc.bank_line_id, alloc.installment_id))
                slot = books.setdefault((alloc.policy_number, alloc.currency), {
                    "net": ZERO, "gross": ZERO, "expected_commission": ZERO,
                    "rates": set(), "bank_line_ids": set(),
                })
                slot["net"] += alloc.amount
                if fig is not None:
                    slot["gross"] += fig["gross"]
                    slot["expected_commission"] += fig["expected_commission"]
                    slot["rates"] |= fig["rates"]
                slot["bank_line_ids"].add(alloc.bank_line_id)
            elif state == "proposed":
                bridge_add(alloc.currency, "at_gate", alloc.amount)
            elif not pack.pack_id.startswith("PACK-COMB"):
                # A rejected ordinary pack's rows stand in the
                # index (its cash never left it); a rejected
                # combination's rows were surrendered when its
                # legs went back to suspense — the index, and so
                # this bridge, holds neither hide nor hair of it.
                bridge_add(alloc.currency, "on_rejected", alloc.amount)

    # The month's by-broker day splits — the index the daily
    # reports read, summed for this broker.
    splits: dict[str, Decimal] = {}
    day = first
    while day <= last:
        if store.bank_lines(on=day):
            rep = day_report(store, day, policy)
            bucket = (rep.get("by_broker") or {}).get(broker_id)
            if bucket:
                for ccy, sub in (bucket.get("currencies") or {}).items():
                    splits[ccy] = splits.get(ccy, ZERO) + Decimal(sub["allocated"])
        day = date.fromordinal(day.toordinal() + 1)

    # The statement, grouped per (policy, currency).
    claimed: dict[tuple[str, str], dict] = {}
    for line in stmt.lines:
        slot = claimed.setdefault((line.policy_number, line.currency), {
            "gross": ZERO, "commission": ZERO, "net": ZERO, "references": [],
        })
        slot["gross"] += line.gross
        slot["commission"] += line.commission
        slot["net"] += line.net
        if line.reference:
            slot["references"].append(line.reference)

    open_exceptions = [e for e in store.exceptions() if e.status == "open"]

    def mapped_exception(policy_number: str) -> dict | None:
        candidates = []
        for e in open_exceptions:
            if e.policy_number != policy_number:
                continue
            if e.broker_id == broker_id:
                candidates.append(e)
            elif e.bank_line_id and e.bank_line_id in lines \
                    and _broker_key(store, lines[e.bank_line_id], names) == broker_id:
                candidates.append(e)
        if not candidates:
            return None
        chosen = sorted(candidates, key=lambda e: (e.first_seen, e.exception_id))[0]
        return {
            "exception_id": chosen.exception_id,
            "code": chosen.code.value,
            "assignee": chosen.assignee,
            "notes": len(chosen.handling_notes),
        }

    def books_view(slot: dict) -> dict:
        return {
            "gross": as_str(slot["gross"]),
            "net": as_str(slot["net"]),
            "expected_commission": as_str(slot["expected_commission"]),
            "contracted_rates": sorted(slot["rates"]),
        }

    rows: list[dict] = []
    for (policy_number, ccy), claim in sorted(claimed.items()):
        book = books.get((policy_number, ccy))
        statement_view = {
            "gross": as_str(claim["gross"]),
            "commission": as_str(claim["commission"]),
            "net": as_str(claim["net"]),
            "references": list(claim["references"]),
        }
        if book is None or book["net"] == ZERO:
            if policy_number in receivables:
                detail = (
                    f"{policy_number} is on the books, but no cash from "
                    f"{broker.name} was applied to it in {period} — the "
                    "broker's claimed remittance never landed as applied "
                    "cash here. (Cash standing in suspense is outside this "
                    "report; see the boundary note.)"
                )
            else:
                detail = (
                    f"{policy_number} is not on the books, and no bank line "
                    f"in {period} carries this remittance — the broker says "
                    f"it paid {as_str(claim['net'])} that the books never "
                    "received as applied cash."
                )
            rows.append({
                "policy_number": policy_number, "currency": ccy,
                "class": "MISSING_ON_BOOKS",
                "statement": statement_view,
                "books": books_view(book) if book is not None else None,
                "detail": detail,
                "exception": mapped_exception(policy_number),
                "bank_line_ids": sorted(book["bank_line_ids"]) if book else [],
            })
            continue
        if claim["gross"] == book["gross"] \
                and claim["commission"] == book["expected_commission"] \
                and claim["net"] == book["net"]:
            cls = "AGREED"
            detail = (
                "the broker's gross, commission and net all agree with the "
                f"books: {as_str(book['net'])} applied"
                + (f" on {', '.join(sorted(book['bank_line_ids']))}" if book["bank_line_ids"] else "")
                + ", to the cent."
            )
        elif claim["gross"] == book["gross"] \
                and claim["commission"] != book["expected_commission"]:
            cls = "COMMISSION_DIFFERS"
            rates = ", ".join(_pct(Decimal(r)) for r in sorted(book["rates"])) or "—"
            implied = (
                f" (an implied {_pct(claim['commission'] / claim['gross'])})"
                if claim["gross"] else ""
            )
            diff = book["net"] - claim["net"]
            direction = (
                f"the broker's net is {as_str(abs(diff))} less than the "
                "contracted net" if diff > ZERO else
                f"the broker's net is {as_str(abs(diff))} more than the "
                "contracted net" if diff < ZERO else
                "the nets agree, but the commission taken does not"
            )
            detail = (
                f"the gross agrees at {as_str(book['gross'])}, but the "
                f"statement takes commission {as_str(claim['commission'])}"
                f"{implied} against a contracted {rates} in force — "
                f"expected commission {as_str(book['expected_commission'])}; "
                f"{direction}."
            )
        else:
            cls = "AMOUNT_DIFFERS"
            bits = []
            if claim["gross"] != book["gross"]:
                bits.append(
                    f"gross differs — statement {as_str(claim['gross'])} "
                    f"against books {as_str(book['gross'])}")
            if claim["net"] != book["net"]:
                bits.append(
                    f"net differs — statement {as_str(claim['net'])} "
                    f"against books {as_str(book['net'])}")
            if claim["gross"] - claim["commission"] != claim["net"]:
                bits.append(
                    "the statement's own net does not foot its gross less "
                    "its commission")
            detail = "; ".join(bits) + "."
        rows.append({
            "policy_number": policy_number, "currency": ccy, "class": cls,
            "statement": statement_view,
            "books": books_view(book),
            "detail": detail,
            "exception": mapped_exception(policy_number),
            "bank_line_ids": sorted(book["bank_line_ids"]),
        })

    for (policy_number, ccy), book in sorted(books.items()):
        if (policy_number, ccy) in claimed or book["net"] == ZERO:
            continue
        rows.append({
            "policy_number": policy_number, "currency": ccy,
            "class": "MISSING_ON_STATEMENT",
            "statement": None,
            "books": books_view(book),
            "detail": (
                f"the books applied {as_str(book['net'])} from {broker.name} "
                f"to {policy_number} in {period} (gross "
                f"{as_str(book['gross'])}); the statement never mentions "
                "the policy."
            ),
            "exception": mapped_exception(policy_number),
            "bank_line_ids": sorted(book["bank_line_ids"]),
        })

    rows.sort(key=lambda r: (r["policy_number"], r["currency"]))
    counts = {cls: 0 for cls in CLASSES}
    for r in rows:
        counts[r["class"]] += 1

    currencies = sorted(
        {ccy for _p, ccy in claimed} | {ccy for _p, ccy in books} | set(splits)
    )
    totals: dict[str, dict] = {}
    for ccy in currencies:
        stmt_by_class = {cls: ZERO for cls in CLASSES}
        books_by_class = {cls: ZERO for cls in CLASSES}
        statement_total = ZERO
        books_total = ZERO
        for r in rows:
            if r["currency"] != ccy:
                continue
            if r["statement"] is not None:
                amt = Decimal(r["statement"]["net"])
                stmt_by_class[r["class"]] += amt
                statement_total += amt
            if r["books"] is not None and r["class"] != "MISSING_ON_BOOKS":
                amt = Decimal(r["books"]["net"])
                books_by_class[r["class"]] += amt
                books_total += amt
        b = bridge.get(ccy, {"posted": ZERO, "at_gate": ZERO, "on_rejected": ZERO})
        split_total = splits.get(ccy, ZERO)
        bridge_total = b["posted"] + b["at_gate"] + b["on_rejected"]
        totals[ccy] = {
            "statement_net_total": as_str(statement_total),
            "statement_by_class": {k: as_str(v) for k, v in stmt_by_class.items()},
            "statement_foots": sum(stmt_by_class.values(), ZERO) == statement_total,
            "books_net_total": as_str(books_total),
            "books_by_class": {k: as_str(v) for k, v in books_by_class.items()},
            "books_foots": sum(books_by_class.values(), ZERO) == books_total,
            "posted": as_str(b["posted"]),
            "at_gate": as_str(b["at_gate"]),
            "on_rejected_packs": as_str(b["on_rejected"]),
            "day_split_allocated": as_str(split_total),
            "splits_difference": as_str(bridge_total - split_total),
            "foots_to_day_splits": bridge_total == split_total,
        }

    foots = all(
        t["statement_foots"] and t["books_foots"] and t["foots_to_day_splits"]
        for t in totals.values()
    )
    if foots:
        footing_note = (
            "The reconciliation FOOTS: per currency, the statement's net "
            "total is the sum of its classes, the books total is the sum of "
            "its classes, and the books' posted cash plus what stands at "
            "the gate and on rejected packs equals the month's by-broker "
            "day splits, to the cent."
        )
    else:
        bits = []
        for ccy, t in sorted(totals.items()):
            if not t["foots_to_day_splits"]:
                bits.append(
                    f"in {ccy} the books' gate-state terms miss the day "
                    f"splits by {t['splits_difference']}")
            if not t["statement_foots"]:
                bits.append(f"in {ccy} the statement classes do not sum to the statement total")
            if not t["books_foots"]:
                bits.append(f"in {ccy} the books classes do not sum to the books total")
        footing_note = "NOT FOOTED: " + "; ".join(bits) + "."

    return {
        "broker_id": broker_id,
        "broker_name": broker.name,
        "period": period,
        "statement_id": stmt.statement_id,
        "source_name": stmt.source_name,
        "statement_lines": len(stmt.lines),
        "counts": counts,
        "rows": rows,
        "totals": totals,
        "foots": foots,
        "footing_note": footing_note,
        "boundary_note": BOUNDARY_NOTE,
    }


def reconciliation_summaries(store: Store, year: int, month: int) -> list[dict]:
    """One summary per broker with a standing statement for the
    month — the close pack's citation of this reconciliation. An
    empty list on a month with no statements, so statement-less
    months' packs stay exactly as they were."""
    period = f"{year:04d}-{month:02d}"
    out = []
    for stmt in sorted(
        store.broker_statements(period=period),
        key=lambda s: (s.broker_id, s.statement_id),
    ):
        if stmt.superseded_by is not None:
            continue
        body = build_reconciliation(store, stmt.broker_id, period)
        out.append({
            "broker_id": body["broker_id"],
            "broker_name": body["broker_name"],
            "statement_id": body["statement_id"],
            "counts": body["counts"],
            "totals": {
                ccy: {
                    "statement_net_total": t["statement_net_total"],
                    "books_net_total": t["books_net_total"],
                }
                for ccy, t in body["totals"].items()
            },
            "foots": body["foots"],
        })
    return out


# ---------------------------------------------------------------------
# Export — digested, wall-clock-free, byte-identical on regeneration
# ---------------------------------------------------------------------

def _digest(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def broker_reconciliation_export(
    store: Store, broker_id: str, period: str,
    policy: PolicyConfig | None = None,
) -> dict:
    body = build_reconciliation(store, broker_id, period, policy)
    return {
        "broker_id": broker_id,
        "period": period,
        "sha256": _digest(body),
        "reconciliation": body,
    }


def render_broker_reconciliation_text(export: dict) -> str:
    body = export["reconciliation"]
    out = [
        f"BROKER STATEMENT RECONCILIATION — {body['broker_id']} · {body['period']}",
        f"Statement {body['statement_id']} ({body['source_name'] or 'unnamed source'})"
        f" · SHA-256 {export['sha256']}",
        "",
        f"{body['broker_name']} states {body['statement_lines']} line(s) for the period.",
    ]
    for ccy, t in sorted(body["totals"].items()):
        out.append(
            f"  {ccy}: statement net {t['statement_net_total']} · books applied "
            f"{t['books_net_total']} · day splits {t['day_split_allocated']} "
            f"(posted {t['posted']} + at the gate {t['at_gate']} + on rejected "
            f"packs {t['on_rejected_packs']})"
        )
    counts = body["counts"]
    out += [
        "",
        "COUNTS: " + " · ".join(f"{cls} {counts[cls]}" for cls in CLASSES),
    ]
    for cls in CLASSES:
        cls_rows = [r for r in body["rows"] if r["class"] == cls]
        if not cls_rows:
            continue
        out += ["", f"{cls} ({len(cls_rows)})"]
        for r in cls_rows:
            s = r["statement"]
            b = r["books"]
            figures = []
            if s is not None:
                figures.append(
                    f"statement gross {s['gross']} / commission {s['commission']} "
                    f"/ net {s['net']}")
            if b is not None:
                figures.append(
                    f"books gross {b['gross']} / expected commission "
                    f"{b['expected_commission']} / net {b['net']}")
            out.append(f"  {r['policy_number']} ({r['currency']}): " + " · ".join(figures))
            out.append(f"    {r['detail']}")
            if r["exception"] is not None:
                e = r["exception"]
                out.append(
                    f"    open exception {e['exception_id']} ({e['code']})"
                    + (f" — assigned to {e['assignee']}" if e["assignee"] else " — unassigned")
                    + f", {e['notes']} note(s); work it in the workbench."
                )
    out += [
        "",
        "FOOTING",
        f"  {body['footing_note']}",
        "",
        f"Boundary: {body['boundary_note']}",
        "",
    ]
    return "\n".join(out)
