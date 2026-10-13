"""Combination settlements: two wires that together settle one bill.

The per-line tiers work one bank line at a time, and a payer who
settles a bill in two wires defeats them honestly: neither wire
equals anything due, no advice explains either, and an exact
subset search over the broker's open items finds nothing — so
each wire lands in suspense as UNIDENTIFIED, even when the second
wire's arrival makes the pair's meaning plain. This pass is the
governed second look. It runs after the per-line tiers (at the end
of a day run, and at the end of the suspense re-match pass) and
looks for pairs the CODE can prove:

- both legs are wholly unapplied cash of the SAME payer — an open
  suspense item whose bank line carries no allocations, holding
  the line's full amount;
- the target is one open installment on that payer's own book (the
  broker's book for a broker payer; the insured's own direct
  policies for a direct payer — combinations never cross payers,
  and never cross brokers);
- the pair settles the installment EXACTLY: each leg is priced
  with the commission rate in force on its own value date (the
  gate's own gross-up arithmetic, `cents(leg / (1 − rate))`), and
  the two gross values sum to the installment's open billed
  balance to the cent. A pair a cent off is not a combination.

A proven pair becomes a PROPOSAL — a `PACK-COMB-<date>` posting
pack at the same human gate, its two allocation entries itemising
the legs (bank line, date, amount, the rate each was priced at).
Nothing is auto-applied and nothing is force-matched: approval
applies both legs atomically or not at all (the gate re-verifies
the pair against the installment's standing balance before it
writes anything), and rejection frees the legs — their suspense
items re-open and the pair can be proposed again when the books
change. From proposal, a leg's suspense item is held resolved,
exactly as a re-match recovery holds its item: the cash is spoken
for, and the day's footing counts it as such.

The search is bounded and says so: pairs only (never triples),
same-payer wholly-unapplied legs only, at most MAX_LEGS_PER_PAYER
legs considered per payer (the oldest, by receipt), targets
walked in policy/sequence order, first proof wins. A desk whose
book needs wider combinations works them by hand at the gate —
the pass exists for the common case, provable to the cent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from . import webhooks
from .matcher import _norm
from .models import (
    Allocation,
    AuditEvent,
    BankLine,
    PostingEntry,
    PostingPack,
    SuspenseItem,
)
from .money import ZERO, cents
from .store import Store

MAX_LEGS_PER_PAYER = 12
COMBINATION_BACKEND = "combination-pass"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _leg_gross(amount: Decimal, leg_date: date, broker, brokers) -> Decimal:
    """A leg's value against the billed balance: the cash grossed
    up at the commission rate in force on the leg's own value
    date — the gate's apply arithmetic, run in advance. Direct
    business has no commission to gross up."""
    if broker is None:
        return amount
    rate = broker.rate_on(leg_date)
    return cents(amount / (1 - rate))


def _payer_key(line: BankLine, brokers_by_name: dict) -> tuple[str, str | None]:
    """Group legs by payer: the broker whose name the payer matches
    (the matcher's own attribution), else the payer themselves."""
    norm = _norm(line.payer_name)
    for broker in brokers_by_name.values():
        bnorm = _norm(broker.name)
        if bnorm in norm or norm in bnorm:
            return (f"broker:{broker.broker_id}", broker.broker_id)
    return (f"payer:{norm}", None)


def _open_legs(store: Store) -> list[tuple[SuspenseItem, BankLine]]:
    """The leg population: open suspense items holding a whole,
    wholly unapplied bank line. A line carrying an open
    DUPLICATE_PAYMENT exception is excluded by design: its cash is
    suspected of being a second sending of money already applied,
    which is the opposite of proven settlement money, and no pair
    proof can speak to it."""
    lines = {l.bank_line_id: l for l in store.bank_lines()}
    # A line counts as allocated when a pack that has not been
    # rejected carries its allocations. A rejected pack settles
    # nothing — its entries stand as a record of a refused
    # proposal — so its lines are unapplied cash again, which is
    # exactly what makes a rejected combination's legs free.
    allocated = {
        entry.allocation.bank_line_id
        for pack in store.packs() if pack.status.value != "rejected"
        for entry in pack.entries if entry.allocation
    }
    duplicate_lines = {
        e.bank_line_id for e in store.exceptions(status="open")
        if e.code.value == "DUPLICATE_PAYMENT" and e.bank_line_id
    }
    legs = []
    for item in store.suspense_items():
        if item.status != "open":
            continue
        line = lines.get(item.bank_line_id)
        if line is None or line.bank_line_id in allocated:
            continue
        if line.bank_line_id in duplicate_lines:
            continue
        if item.amount != line.amount:
            continue
        legs.append((item, line))
    legs.sort(key=lambda pair: (pair[0].received_date, pair[0].suspense_id))
    return legs


def _committed_leg_ids(store: Store) -> set[str]:
    """Bank lines already standing as legs in a combination pack
    that has not been rejected: spoken for, not re-proposable."""
    committed: set[str] = set()
    for pack in store.packs():
        if not pack.pack_id.startswith("PACK-COMB"):
            continue
        if pack.status.value == "rejected":
            continue
        for entry in pack.entries:
            if entry.combination_id and entry.allocation:
                committed.add(entry.allocation.bank_line_id)
    return committed


def find_combinations(store: Store) -> list[dict]:
    """Every provable pair on the books as they stand. Pure read:
    each result names the two legs, the target installment, and
    the priced arithmetic that proves it."""
    brokers = {b.broker_id: b for b in store.brokers()}
    receivables = {r.policy_number: r for r in store.receivables()}
    committed = _committed_leg_ids(store)

    groups: dict[str, tuple[str | None, list[tuple[SuspenseItem, BankLine]]]] = {}
    for item, line in _open_legs(store):
        if line.bank_line_id in committed:
            continue
        key, broker_id = _payer_key(line, brokers)
        groups.setdefault(key, (broker_id, []))[1].append((item, line))

    found: list[dict] = []
    used_legs: set[str] = set()
    used_targets: set[str] = set()
    for key in sorted(groups):
        broker_id, legs = groups[key]
        legs = legs[:MAX_LEGS_PER_PAYER]
        if len(legs) < 2:
            continue
        broker = brokers.get(broker_id) if broker_id else None
        # The payer's own book: the broker's open policies, or — for
        # a direct payer — the policies billed to that insured.
        if broker is not None:
            book = [r for r in receivables.values() if r.broker_id == broker_id]
        else:
            payer_norm = key.split(":", 1)[1]
            book = [
                r for r in receivables.values()
                if r.broker_id is None and _norm(r.insured_name) == payer_norm
            ]
        targets = []
        for rec in sorted(book, key=lambda r: r.policy_number):
            if rec.status.value == "cancelled":
                continue
            for inst in rec.open_installments():
                targets.append((rec, inst))
        for rec, inst in targets:
            if inst.installment_id in used_targets:
                continue
            due = inst.open_balance
            available = [
                (item, line) for item, line in legs
                if line.bank_line_id not in used_legs
            ]
            pair = None
            for i in range(len(available)):
                for j in range(i + 1, len(available)):
                    (item_a, line_a), (item_b, line_b) = available[i], available[j]
                    gross_a = _leg_gross(line_a.amount, line_a.value_date, broker, brokers)
                    gross_b = _leg_gross(line_b.amount, line_b.value_date, broker, brokers)
                    if gross_a + gross_b == due:
                        pair = ((item_a, line_a, gross_a), (item_b, line_b, gross_b))
                        break
                if pair:
                    break
            if pair is None:
                continue
            (item_a, line_a, gross_a), (item_b, line_b, gross_b) = pair
            used_legs.update({line_a.bank_line_id, line_b.bank_line_id})
            used_targets.add(inst.installment_id)
            rate_a = broker.rate_on(line_a.value_date) if broker else None
            rate_b = broker.rate_on(line_b.value_date) if broker else None
            found.append({
                "combination_id": f"COMB-{line_a.bank_line_id}+{line_b.bank_line_id}",
                "policy_number": rec.policy_number,
                "installment_id": inst.installment_id,
                "broker_id": broker_id,
                "target_open_balance": due,
                "legs": [
                    {"suspense_id": item_a.suspense_id, "bank_line_id": line_a.bank_line_id,
                     "value_date": line_a.value_date, "amount": line_a.amount,
                     "rate": rate_a, "gross_value": gross_a},
                    {"suspense_id": item_b.suspense_id, "bank_line_id": line_b.bank_line_id,
                     "value_date": line_b.value_date, "amount": line_b.amount,
                     "rate": rate_b, "gross_value": gross_b},
                ],
                "cash_total": line_a.amount + line_b.amount,
            })
    return found


def propose_combinations(store: Store, actor: str, as_of: date) -> dict:
    """Find provable pairs and assemble them into a PACK-COMB pack
    at the gate. Legs' suspense items are held resolved from
    proposal (the re-match discipline); a rejected pack frees
    them again (see gate.reject_pack)."""
    found = find_combinations(store)
    if not found:
        return {"proposed": [], "pack_id": None}

    pack = PostingPack(
        pack_id=f"PACK-COMB-{as_of.isoformat()}", value_date=as_of, assembled_by=actor
    )
    items = {i.suspense_id: i for i in store.suspense_items()}
    for combo in found:
        leg_bits = []
        for leg in combo["legs"]:
            rate_note = (
                f"net of commission at {leg['rate']:.1%} (the rate in force "
                f"on {leg['value_date']})"
                if leg["rate"] is not None else "direct business, no commission"
            )
            leg_bits.append(
                f"{leg['bank_line_id']} ({leg['value_date']}): {leg['amount']} "
                f"cash, {leg['gross_value']} billed, {rate_note}"
            )
        note = (
            f"Combination settlement for {combo['installment_id']}: two wires "
            f"from the same payer together settle the installment's open "
            f"balance {combo['target_open_balance']} exactly — "
            + "; ".join(leg_bits) + ". Proposed by the combination pass; "
            "applies only if the gate approves both legs together."
        )
        for leg in combo["legs"]:
            pack.entries.append(PostingEntry(
                kind="allocation",
                allocation=Allocation(
                    bank_line_id=leg["bank_line_id"],
                    policy_number=combo["policy_number"],
                    installment_id=combo["installment_id"],
                    amount=leg["amount"], tier=None, note=note,
                ),
                proposal_kind="combination",
                proposed_by=COMBINATION_BACKEND,
                combination_id=combo["combination_id"],
            ))
            item = items[leg["suspense_id"]]
            item.status = "resolved"
            item.resolved_on = as_of
            store.save_suspense(item)

    existing = store.pack(pack.pack_id)
    if existing is not None and existing.status.value == "proposed":
        have = {
            e.allocation.bank_line_id for e in existing.entries if e.allocation
        }
        for entry in pack.entries:
            if entry.allocation.bank_line_id not in have:
                existing.entries.append(entry)
        pack = existing
    elif existing is not None:
        n = 2
        while store.pack(f"{pack.pack_id}-{n}") is not None:
            n += 1
        pack.pack_id = f"{pack.pack_id}-{n}"
    store.save_pack(pack)
    store.save_allocations(
        [e.allocation for e in pack.entries if e.allocation], pack.pack_id
    )
    for combo in found:
        store.audit(AuditEvent(
            at=_now(), actor=actor, action="combination_proposed",
            subject=combo["combination_id"],
            detail=f"Legs {combo['legs'][0]['bank_line_id']} "
                   f"({combo['legs'][0]['amount']}) + "
                   f"{combo['legs'][1]['bank_line_id']} "
                   f"({combo['legs'][1]['amount']}) settle "
                   f"{combo['installment_id']} (open balance "
                   f"{combo['target_open_balance']}) exactly; proposed in "
                   f"{pack.pack_id}, stops at the gate",
        ))
    webhooks.emit(store, "pack_assembled", pack.pack_id, actor, {
        "allocations": str(pack.allocation_total),
        "entries": str(len(pack.entries)),
    }, pack_kind="posting")
    return {
        "proposed": [
            {
                "combination_id": c["combination_id"],
                "policy_number": c["policy_number"],
                "installment_id": c["installment_id"],
                "legs": [
                    {"bank_line_id": leg["bank_line_id"],
                     "amount": str(leg["amount"])}
                    for leg in c["legs"]
                ],
                "cash_total": str(c["cash_total"]),
            }
            for c in found
        ],
        "pack_id": pack.pack_id,
    }
