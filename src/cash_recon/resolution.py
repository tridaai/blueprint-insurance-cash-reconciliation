"""Resolution proposals for unidentified cash.

The tiers settle cash whose payer is known. The hardest pile on
the desk is the opposite: a wire nobody can attribute — no usable
reference, a payer name that matches no broker's book and no
insured — parked in suspense as UNIDENTIFIED until a human works
it out. This module is the governed second look at that pile.

**Why the whole book is in scope here — and only here.** A lump
sum from a known broker is settled within that broker's own book,
and a proposal crossing brokers is refused whole (see the
matcher): that rule protects money whose owner is known from
being mixed with another desk's. Unidentified cash has no known
owner — that is the problem being worked — so the book restriction
has nothing to protect. What replaces it is stricter in a
different way: a candidate exists only when it carries **exact
arithmetic** (an interpretation of the amount that foots to the
cent) or **verbatim evidence** from the line's own text (the
policy, insured, or broker it names, quoted) — and candidates
become settlements only when a named approver, at the gate,
chooses one. The scan proposes; it never applies, and no
candidate strength changes that.

**The scan is deterministic and runs first.** For one suspense
item it walks every open receivable denominated in the line's
currency and builds the readings the cash supports:

- `exact` — the amount equals one open installment's billed
  balance;
- `netting` — the amount equals one open installment's cash due
  net of commission at the rate in force on the line's value
  date, at the contracted rate of the policy's own broker of
  record (the candidate names that broker and that rate);
- `installment_run` — the amount equals a run of consecutive
  open installments' cash dues, oldest first (what settling each
  takes: net of commission for broker business, billed for
  direct); a candidate must be applicable whole through the
  gate's cash arithmetic, so residue-leaving readings are not
  candidates;
- `narrative` — the line's own text names the policy (a reference
  the bank mangled past the matcher's strict pattern still
  counts, quoted verbatim) or the insured, and the cash allocates
  across that policy's open installments oldest first.

Evidence found in the text also attaches to arithmetic candidates
on the evidenced policy or book — the case that matters most: two
brokers' rates both make the amount foot, and only the narrative
says which desk sent it. A policy the evidence names but whose
cash cannot be applied — cancelled, or nothing open — yields a
**flagged** candidate: on the record as considered, never
choosable.

**The model's place is ranking, not invention.** A connected
reader may rank the scan's candidates and explain the ranking,
and may point at a policy the scan missed only by quoting the
line's own text verbatim — the citation discipline the remittance
reader already works under; an uncited addition is refused by
validation. The scan's candidates are never removed by the model.
With no provider configured there is no ranking: the deterministic
scan stands alone and the output is labelled heuristic, as
everywhere in this blueprint.

**The pack is a docket of alternates.** Assembly (a named person)
produces a `PACK-RESOLVE-<date>-<suspense-id>` posting pack whose
`resolution` record carries the candidate set; the pack holds no
entries until the gate decides. Approval must name the chosen
candidate: the gate materialises exactly that candidate's
allocations as the pack's entries and applies them through the
ordinary arithmetic (a netting candidate grosses up at its
broker's rate, like any broker cash), resolves the suspense item
and its UNIDENTIFIED exception, and leaves the unchosen
candidates on the pack's record — lapsed, not deleted. Rejection
leaves the item open and the candidates on the rejected pack's
record. The suspense item stays **open** while a pack stands
proposed: unlike a combination's legs, this cash has not been
matched to anything yet — a hypothesis is not a claim — so the
day footing and the aging keep counting it as suspense until the
gate chooses. A policy already standing as a candidate, under the
same interpretation, in another undecided resolution pack cannot
be proposed twice: the assembly refuses, naming the conflict.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal

from . import webhooks
from .fx import FUNCTIONAL_CURRENCY, rate_on, to_functional
from .matcher import _norm, cash_due
from .models import (
    AuditEvent,
    BankLine,
    PostingPack,
    Receivable,
    ResolutionAllocation,
    ResolutionCandidate,
    ResolutionInfo,
    SuspenseItem,
)
from .money import ZERO, as_str
from .store import Store

SCAN_BACKEND = "resolution_scan"

# The matcher's policy pattern is strict (NSP-NNNN). Bank text
# mangles references — "NSP 7001", "nsp-7001" — past recognition,
# and a mangled reference is exactly the kind of evidence a human
# works from: the scan reads it leniently, quotes it verbatim, and
# puts the reading to the gate instead of acting on it.
_LENIENT_POLICY_RE = re.compile(r"(?i)\bNSP[-\s]?(\d{4})\b")
_STRICT_POLICY_RE = re.compile(r"NSP-\d{4}")


class ResolutionError(RuntimeError):
    """A resolution act was refused. The message says why, in words."""


def _stamp(as_of: date) -> datetime:
    return datetime.combine(as_of, time(12, 0), tzinfo=timezone.utc)


# --------------------------------------------------------------------- scope


def _eligible_item(store: Store, suspense_id: str) -> tuple[SuspenseItem, BankLine]:
    """The item the scan may work, or a refusal in words. The scan
    is for cash nobody has identified: wholly unapplied, held as
    UNIDENTIFIED, with its bank line on the books."""
    item = next((i for i in store.suspense_items() if i.suspense_id == suspense_id), None)
    if item is None:
        raise ResolutionError(f"no such suspense item: {suspense_id}")
    if item.status != "open":
        raise ResolutionError(
            f"suspense item {suspense_id} is {item.status}: it holds no open "
            "cash to resolve")
    if item.reason != "UNIDENTIFIED":
        raise ResolutionError(
            f"suspense item {suspense_id} is held as {item.reason}, not "
            "UNIDENTIFIED. The resolution scan is for cash nobody has "
            "identified; cash held under another code has its own governed "
            "paths (the re-match pass, the workbench, a recorded return)")
    line = next((l for l in store.bank_lines() if l.bank_line_id == item.bank_line_id), None)
    if line is None:
        raise ResolutionError(
            f"suspense item {suspense_id} names bank line {item.bank_line_id}, "
            "which is not on the books: there is no line text to scan and no "
            "cash to settle")
    if item.amount != line.amount:
        raise ResolutionError(
            f"suspense item {suspense_id} holds {item.amount} of bank line "
            f"{line.bank_line_id}'s {line.amount}: part of this line's cash "
            "has already been worked, so a whole-line resolution cannot be "
            "proposed. The remainder is worked through the re-match pass "
            "and the workbench")
    allocated = {
        entry.allocation.bank_line_id: pack.pack_id
        for pack in store.packs() if pack.status.value != "rejected"
        for entry in pack.entries if entry.allocation
    }
    if line.bank_line_id in allocated:
        raise ResolutionError(
            f"bank line {line.bank_line_id} already stands allocated in pack "
            f"{allocated[line.bank_line_id]}: its cash is spoken for at the "
            "gate, not sitting unidentified")
    return item, line


# ------------------------------------------------------------------ evidence


def _line_texts(line: BankLine) -> list[str]:
    """The line's own words, the only place evidence may come
    from: its reference, its narrative, and the payer name the
    bank reported."""
    return [t for t in (line.reference, line.narrative, line.payer_name) if t]


_NAME_SUFFIXES = {
    "brokers", "broker", "insurance", "llc", "inc", "ltd", "limited",
    "group", "co", "company", "the",
}


def _name_forms(name: str) -> list[str]:
    """The ways a name may appear in bank text: as written, with
    its ampersand spelled out, and with trailing corporate suffixes
    dropped ("Birch & Vale Brokers" is remitted as "Birch & Vale").
    Only forms long enough to be distinctive are offered."""
    forms: list[str] = []

    def offer(form: str) -> None:
        form = form.strip(" ,.")
        if len(form) >= 8 and form not in forms:
            forms.append(form)

    offer(name)
    if "&" in name:
        offer(name.replace("&", "and"))
    tokens = name.split()
    while len(tokens) > 1 and tokens[-1].lower().strip(",.") in _NAME_SUFFIXES:
        tokens = tokens[:-1]
        offer(" ".join(tokens))
        if "&" in " ".join(tokens):
            offer(" ".join(tokens).replace("&", "and"))
    return forms


def _name_span(texts: list[str], name: str) -> str | None:
    """The verbatim span of `texts` that names `name`, if any: the
    quote must be the text as it stands, not the name as booked."""
    for form in _name_forms(name):
        for text in texts:
            idx = text.lower().find(form.lower())
            if idx >= 0:
                return text[idx:idx + len(form)]
    return None


class _Evidence:
    """What the line's text names: policies (by number, strict or
    mangled), insureds, and brokers — each with its verbatim quote."""

    def __init__(self, line: BankLine, receivables: dict[str, Receivable], brokers: dict):
        self.texts = _line_texts(line)
        self.policies: dict[str, str] = {}  # policy number -> verbatim quote
        for text in self.texts:
            for m in _LENIENT_POLICY_RE.finditer(text):
                self.policies.setdefault(f"NSP-{m.group(1)}", m.group(0))
        self.insureds: dict[str, str] = {}  # policy number -> verbatim quote
        for rec in receivables.values():
            span = _name_span(self.texts, rec.insured_name)
            if span:
                self.insureds[rec.policy_number] = span
        self.brokers: dict[str, str] = {}  # broker id -> verbatim quote
        for broker in brokers.values():
            span = _name_span(self.texts, broker.name)
            if span:
                self.brokers[broker.broker_id] = span

    def for_policy(self, rec: Receivable) -> tuple[str, str]:
        """(quote, kind): the strongest evidence naming this
        policy — its number first, then its insured, then its
        broker of record's name in the text."""
        if rec.policy_number in self.policies:
            return self.policies[rec.policy_number], "policy_number"
        if rec.policy_number in self.insureds:
            return self.insureds[rec.policy_number], "insured_name"
        if rec.broker_id and rec.broker_id in self.brokers:
            return self.brokers[rec.broker_id], "broker_name"
        return "", ""


# ---------------------------------------------------------------- candidates


def _alloc(line: BankLine, policy_number: str, installment_id: str,
           amount: Decimal) -> ResolutionAllocation:
    """One candidate allocation, with the conversion recorded as
    the matcher records it: at the governed rate in force on the
    bank line's value date, never re-priced later."""
    spec = ResolutionAllocation(
        policy_number=policy_number, installment_id=installment_id, amount=amount)
    if line.currency != FUNCTIONAL_CURRENCY:
        spec.fx_rate = rate_on(line.currency, line.value_date)
        spec.functional_amount = to_functional(amount, line.currency, line.value_date)
    return spec


def _narrative_allocations(line: BankLine, rec: Receivable,
                           brokers: dict) -> list[ResolutionAllocation] | None:
    """The reference-led reading of the cash against one policy:
    oldest open installment first, on the cash basis — each
    installment takes its cash due (net of commission for broker
    business, the billed balance for direct) — the whole amount,
    or nothing (cash beyond what the policy's installments can
    take is not this policy's cash alone, and a candidate must
    dispose of the whole item through the gate's cash arithmetic
    without leaving residue)."""
    insts = rec.open_installments()
    dues = [cash_due(rec, i, brokers, line.value_date) for i in insts]
    if sum(dues, ZERO) < line.amount:
        return None
    out, remaining = [], line.amount
    for inst, due in zip(insts, dues):
        if remaining <= ZERO:
            break
        take = min(remaining, due)
        if take > ZERO:
            out.append(_alloc(line, rec.policy_number, inst.installment_id, take))
            remaining -= take
    return out if remaining == ZERO else None


def _deterministic_candidates(store: Store, line: BankLine,
                              receivables: dict[str, Receivable],
                              brokers: dict) -> list[ResolutionCandidate]:
    """The scan proper: every reading of the cash the books
    support, in a fixed order (policy, then interpretation), each
    carrying its arithmetic and whatever evidence the line's text
    holds for it."""
    evidence = _Evidence(line, receivables, brokers)
    amount = line.amount
    found: list[ResolutionCandidate] = []
    seen: set[tuple] = set()

    def add(rec: Receivable, interpretation: str,
            allocations: list[ResolutionAllocation], arithmetic: str) -> None:
        key = tuple((a.installment_id, str(a.amount)) for a in allocations)
        if key in seen:
            return
        seen.add(key)
        quote, kind = evidence.for_policy(rec)
        found.append(ResolutionCandidate(
            candidate_id="", policy_number=rec.policy_number,
            broker_id=rec.broker_id, interpretation=interpretation,
            allocations=allocations, arithmetic=arithmetic,
            evidence=quote, evidence_kind=kind,
        ))

    for rec in sorted(receivables.values(), key=lambda r: r.policy_number):
        if rec.currency != line.currency or rec.status.value == "cancelled":
            continue
        insts = rec.open_installments()
        if not insts:
            continue
        broker = brokers.get(rec.broker_id) if rec.broker_id else None
        rate = broker.rate_on(line.value_date) if broker else None
        # exact: one installment's billed balance
        for inst in insts:
            if inst.open_balance == amount:
                add(rec, "exact",
                    [_alloc(line, rec.policy_number, inst.installment_id, amount)],
                    f"{inst.installment_id} of {rec.policy_number} stands open at "
                    f"{inst.open_balance} billed; the wire is {amount} — the "
                    "billed amount, to the cent.")
                break
        # netting: one installment's cash due at its broker's rate
        if broker is not None:
            for inst in insts:
                due = cash_due(rec, inst, brokers, line.value_date)
                if due == amount:
                    add(rec, "netting",
                        [_alloc(line, rec.policy_number, inst.installment_id, amount)],
                        f"{inst.installment_id} of {rec.policy_number} is billed "
                        f"{inst.open_balance}; {broker.name} is the policy's broker "
                        f"of record, and its contracted rate in force on "
                        f"{line.value_date} is {rate:.1%} — net due {due}; the "
                        f"wire is {amount}, to the cent.")
                    break
        # installment runs, oldest first, on the cash basis: what
        # settling each installment takes (net of commission for
        # broker business, billed for direct). A run is a
        # candidate only if the gate's cash arithmetic can apply
        # it whole — readings that would leave residue are the
        # matcher's in-pipeline business, not a docket's.
        dues = [cash_due(rec, i, brokers, line.value_date) for i in insts]
        label = "net of commission" if broker else "billed"
        run = ZERO
        for k, due in enumerate(dues):
            run += due
            if run == amount and k >= 1:
                names = ", ".join(i.installment_id for i in insts[:k + 1])
                parts = " + ".join(str(d) for d in dues[:k + 1])
                add(rec, "installment_run",
                    [_alloc(line, rec.policy_number, insts[j].installment_id, dues[j])
                     for j in range(k + 1)],
                    f"A run of {k + 1} consecutive open installments of "
                    f"{rec.policy_number} ({names}), {label}: {parts} = "
                    f"{amount} — the wire, to the cent.")
                break
            if run > amount:
                break
        # narrative: the text names this policy or its insured
        if rec.policy_number in evidence.policies or rec.policy_number in evidence.insureds:
            allocs = _narrative_allocations(line, rec, brokers)
            if allocs is not None:
                quote, kind = evidence.for_policy(rec)
                who = ("policy number" if kind == "policy_number" else "insured")
                flow = "; ".join(
                    f"{a.amount} to {a.installment_id}" for a in allocs)
                add(rec, "narrative", allocs,
                    f"The wire's own text names this policy's {who} "
                    f"(\u201c{quote}\u201d). The cash allocates across "
                    f"{rec.policy_number}'s open installments oldest first: "
                    f"{flow} — the reference-led reading, proposed for the "
                    "gate's decision.")

    # Flagged: the evidence names a policy whose cash cannot be
    # applied. On the record as considered; never choosable.
    named = set(evidence.policies) | set(evidence.insureds)
    for number in sorted(named):
        rec = receivables.get(number)
        if rec is None:
            continue
        quote, kind = evidence.for_policy(rec)
        if rec.status.value == "cancelled":
            found.append(ResolutionCandidate(
                candidate_id="", policy_number=number, broker_id=rec.broker_id,
                interpretation="narrative", evidence=quote, evidence_kind=kind,
                flags=["CANCELLED_POLICY"],
                arithmetic=f"The wire's text names {number} "
                           f"(\u201c{quote}\u201d), which was cancelled before "
                           "this cash arrived. Cash for a cancelled policy is "
                           "a human call — it meets any return-premium payable "
                           "first — so this reading stands as considered and "
                           "flagged, and the gate cannot choose it."))
        elif rec.currency != line.currency:
            found.append(ResolutionCandidate(
                candidate_id="", policy_number=number, broker_id=rec.broker_id,
                interpretation="narrative", evidence=quote, evidence_kind=kind,
                flags=["OTHER_CURRENCY"],
                arithmetic=f"The wire's text names {number} "
                           f"(\u201c{quote}\u201d), which is billed in "
                           f"{rec.currency}; this cash arrived in "
                           f"{line.currency}, and premium cash is never "
                           "converted to settle another currency's policy. "
                           "Considered and flagged; the gate cannot choose it."))
        elif not rec.open_installments():
            found.append(ResolutionCandidate(
                candidate_id="", policy_number=number, broker_id=rec.broker_id,
                interpretation="narrative", evidence=quote, evidence_kind=kind,
                flags=["NO_OPEN_BALANCE"],
                arithmetic=f"The wire's text names {number} "
                           f"(\u201c{quote}\u201d), which has no open balance: "
                           "there is nothing for this cash to settle. "
                           "Considered and flagged; the gate cannot choose it."))
    return found


def _apply_model_ranking(store: Store, reader, line: BankLine,
                         receivables: dict[str, Receivable],
                         candidates: list[ResolutionCandidate]) -> bool:
    """Let a connected reader rank the scan and point at what it
    missed. Returns True when the model's work was admitted. The
    deterministic candidates are never removed or altered; ranking
    is metadata on them, and an added policy is built by the same
    narrative rule as the scan's own — the model points, code
    constructs, and only on verbatim evidence."""
    rank = getattr(reader, "rank_resolution_candidates", None)
    if rank is None:
        return False
    decidable = [c for c in candidates if not c.flags]
    line_text = "\n".join(_line_texts(line))
    result = rank(line.amount, line.payer_name, line_text, [
        {"candidate_id": c.candidate_id, "policy_number": c.policy_number,
         "interpretation": c.interpretation, "arithmetic": c.arithmetic,
         "evidence": c.evidence}
        for c in decidable
    ])
    if not result:
        return False
    by_id = {c.candidate_id: c for c in decidable}
    for position, cid in enumerate(result.get("ranked", []), start=1):
        cand = by_id.get(cid)
        if cand is not None and cand.model_rank is None:
            cand.model_rank = position
            cand.model_explanation = result.get("explanations", {}).get(cid, "")
    admitted = bool(result.get("ranked"))
    for extra in result.get("added", []):
        rec = receivables.get(extra["policy_number"])
        if rec is None or rec.currency != line.currency:
            continue
        if any(c.policy_number == rec.policy_number for c in candidates):
            continue
        if rec.status.value == "cancelled":
            candidates.append(ResolutionCandidate(
                candidate_id="", policy_number=rec.policy_number,
                broker_id=rec.broker_id, interpretation="narrative",
                evidence=extra["evidence"], evidence_kind="policy_number",
                proposed_by=getattr(reader, "name", "llm"),
                flags=["CANCELLED_POLICY"],
                arithmetic=f"Named by the model from the wire's text "
                           f"(\u201c{extra['evidence']}\u201d); {rec.policy_number} "
                           "was cancelled before this cash arrived, so the "
                           "reading stands as considered and flagged."))
            admitted = True
            continue
        if not rec.open_installments():
            continue
        brokers = {b.broker_id: b for b in store.brokers()}
        allocs = _narrative_allocations(line, rec, brokers)
        if allocs is None:
            continue
        flow = "; ".join(f"{a.amount} to {a.installment_id}" for a in allocs)
        candidates.append(ResolutionCandidate(
            candidate_id="", policy_number=rec.policy_number,
            broker_id=rec.broker_id, interpretation="narrative",
            allocations=allocs, evidence=extra["evidence"],
            evidence_kind="policy_number",
            proposed_by=getattr(reader, "name", "llm"),
            arithmetic=f"Named by the model from the wire's own text "
                       f"(\u201c{extra['evidence']}\u201d), which the "
                       "deterministic scan did not read as a reference. The "
                       "cash allocates across the policy's open installments "
                       f"oldest first: {flow}."))
        admitted = True
    return admitted


def _scan(store: Store, reader, item: SuspenseItem, line: BankLine) -> dict:
    """The scan itself: deterministic candidates first, the
    model's ranking admitted second, ids assigned last (model-added
    candidates take the next numbers, so the record's numbering is
    the order the docket presents)."""
    receivables = {r.policy_number: r for r in store.receivables()}
    brokers = {b.broker_id: b for b in store.brokers()}
    candidates = _deterministic_candidates(store, line, receivables, brokers)
    for i, cand in enumerate(candidates, start=1):
        cand.candidate_id = f"C{i}"
    ranked = _apply_model_ranking(store, reader, line, receivables, candidates)
    # Model-added candidates were appended without ids; number them
    # after the scan's own, in the order they were admitted.
    nxt = sum(1 for c in candidates if c.candidate_id) + 1
    for cand in candidates:
        if not cand.candidate_id:
            cand.candidate_id = f"C{nxt}"
            nxt += 1
    backend = SCAN_BACKEND + (f"+model:{getattr(reader, 'name', '')}" if ranked else "")
    return {
        "suspense_id": item.suspense_id,
        "bank_line_id": line.bank_line_id,
        "amount": as_str(item.amount),
        "currency": item.currency,
        "scan_backend": backend,
        "model_ranked": ranked,
        "heuristic": not ranked,
        "candidates": [json.loads(c.model_dump_json()) for c in candidates],
        "_candidate_objects": candidates,
    }


def _public(scan: dict) -> dict:
    return {k: v for k, v in scan.items() if not k.startswith("_")}


def _audit_scan(store: Store, actor: str, as_of: date, scan: dict) -> None:
    store.audit(AuditEvent(
        at=_stamp(as_of), actor=actor, action="suspense_resolution_scan",
        subject=scan["suspense_id"],
        detail=json.dumps({
            "bank_line_id": scan["bank_line_id"],
            "amount": scan["amount"],
            "currency": scan["currency"],
            "scan_backend": scan["scan_backend"],
            "candidates": [
                {"candidate_id": c["candidate_id"], "policy_number": c["policy_number"],
                 "interpretation": c["interpretation"], "evidence": c["evidence"],
                 "flags": c["flags"], "proposed_by": c["proposed_by"]}
                for c in scan["candidates"]
            ],
        })))


# ---------------------------------------------------------------------- acts


def scan_candidates(store: Store, reader, suspense_id: str, actor: str,
                    as_of: date) -> dict:
    """Run the resolution scan for one unidentified item and put
    it on the item's audit trail. A read of the books as they stand
    plus one audit event — the scan decides nothing."""
    item, line = _eligible_item(store, suspense_id)
    scan = _scan(store, reader, item, line)
    _audit_scan(store, actor, as_of, scan)
    return _public(scan)


def assemble_resolution_pack(store: Store, reader, suspense_id: str,
                             actor: str, as_of: date) -> PostingPack:
    """Assemble the resolution docket for one item: a fresh scan,
    carried as alternates on a PACK-RESOLVE pack at the gate.

    Refused, in words, when the item is out of scope, when the
    scan finds nothing decidable (nothing to put to the gate), when
    a resolution pack for this item already stands undecided, or
    when a candidate would double-propose a policy already standing
    under the same interpretation in another undecided resolution
    pack. The suspense item stays open: the pack is a docket of
    hypotheses, not a claim on the cash."""
    if not actor or not actor.strip():
        raise ResolutionError("a resolution pack is assembled by a named person")
    item, line = _eligible_item(store, suspense_id)
    for pack in store.packs():
        if (pack.resolution is not None
                and pack.resolution.suspense_id == suspense_id
                and pack.status.value == "proposed"):
            raise ResolutionError(
                f"resolution pack {pack.pack_id} already stands proposed for "
                f"suspense item {suspense_id}: decide it at the gate, or "
                "reject it, before scanning again")
    scan = _scan(store, reader, item, line)
    candidates: list[ResolutionCandidate] = scan["_candidate_objects"]
    decidable = [c for c in candidates if not c.flags]
    if not decidable:
        flagged = [f"{c.policy_number} ({', '.join(c.flags)})" for c in candidates if c.flags]
        _audit_scan(store, actor, as_of, scan)
        raise ResolutionError(
            f"the scan found no defensible candidate for suspense item "
            f"{suspense_id}: no open item in {line.currency} matches the "
            f"wire's {item.amount} by exact arithmetic, and the line's text "
            "names nothing the cash can settle"
            + (f". Considered and flagged: {'; '.join(flagged)}." if flagged else ".")
            + " There is nothing to put to the gate; the cash stays in "
            "suspense for the workbench")
    for pack in store.packs():
        if pack.resolution is None or pack.status.value != "proposed":
            continue
        standing = {
            (c.policy_number, c.interpretation)
            for c in pack.resolution.candidates if not c.flags
        }
        for cand in decidable:
            if (cand.policy_number, cand.interpretation) in standing:
                raise ResolutionError(
                    f"candidate {cand.candidate_id} would double-propose "
                    f"{cand.policy_number} under the {cand.interpretation} "
                    f"reading: it already stands in undecided resolution "
                    f"pack {pack.pack_id} (for suspense item "
                    f"{pack.resolution.suspense_id}). Decide that pack "
                    "first — one reading of a policy is put to the gate "
                    "once at a time")
    _audit_scan(store, actor, as_of, scan)
    pack_id = f"PACK-RESOLVE-{as_of.isoformat()}-{suspense_id}"
    if store.pack(pack_id) is not None:
        n = 2
        while store.pack(f"{pack_id}-{n}") is not None:
            n += 1
        pack_id = f"{pack_id}-{n}"
    pack = PostingPack(
        pack_id=pack_id, value_date=as_of, assembled_by=actor,
        resolution=ResolutionInfo(
            suspense_id=item.suspense_id, bank_line_id=line.bank_line_id,
            amount=item.amount, currency=item.currency,
            scan_backend=scan["scan_backend"], candidates=candidates,
        ),
    )
    store.save_pack(pack)
    store.audit(AuditEvent(
        at=_stamp(as_of), actor=actor, action="resolution_pack_assembled",
        subject=pack_id,
        detail=f"{len(decidable)} alternate(s) for suspense item {suspense_id} "
               f"({item.amount} {item.currency} from bank line {line.bank_line_id}); "
               "approval must name the chosen candidate; nothing is applied "
               "until it does"))
    store.audit(AuditEvent(
        at=_stamp(as_of), actor=actor, action="suspense_resolution_pack",
        subject=suspense_id,
        detail=f"resolution pack {pack_id} assembled by {actor}: candidates "
               + ", ".join(
                   f"{c.candidate_id} {c.policy_number}/{c.interpretation}"
                   + (f" [flagged: {', '.join(c.flags)}]" if c.flags else "")
                   for c in candidates)))
    webhooks.emit(store, "pack_assembled", pack_id, actor, {
        "allocations": as_str(item.amount),
        "entries": str(len(decidable)),
    }, pack_kind="posting")
    return pack
