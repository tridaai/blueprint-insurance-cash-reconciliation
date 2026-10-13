"""The per-line audit export: the auditor's artifact for one bank line.

`returns.line_history` already gathers a line's chain for the desk.
This is the formal version of that idea — the document an auditor
is handed. For one bank line it traces every cent and every act:

- the line as received, with its provenance (dataset, statement
  import with its batch id, or manual);
- the tiers attempted and their outcomes, in order — read back
  from the attempt records the matcher persisted to the audit
  trail as it worked (a line matched before attempt recording says
  so, in words, rather than inventing a ladder);
- every allocation with its basis (gross / net of commission /
  proposal), its pack, and any governed edit beside the original;
- every exception raised against the line, with its handling
  record, and every re-match attempt on its suspense;
- the gate history of every pack that carried the line —
  assembled by, edits, approvals and rejections by name;
- returns and re-sends, linked both ways;
- and a footing block that accounts for the line to the cent
  inside the export itself — or says, in words, that it does not
  foot and by how much.

The export is a read model in the close pack's mould: the body
carries no wall-clock stamps (every timestamp in it is a stored
record), a SHA-256 digest covers the canonical body, and the JSON
export and the text rendering are two views of one record.
Building it mutates nothing.
"""

from __future__ import annotations

import json

from .close_pack import pack_digest
from .money import ZERO, as_str
from .store import Store, dump
from .models import Tier


class LineExportError(RuntimeError):
    """The export was refused. The message says why."""


def _json(model) -> dict:
    return json.loads(dump(model))


def _basis(alloc) -> str:
    """The basis an allocation was made on, from its tier and the
    note the matcher wrote on it: gross (the billed amount) or net
    of commission (the cash due)."""
    note = (alloc.note or "").lower()
    if "net basis" in note:
        return "net of commission"
    if "gross basis" in note:
        return "gross (billed amount)"
    if alloc.tier == Tier.T1_EXACT:
        return "gross (billed amount)"
    if alloc.tier == Tier.T2_NETTING:
        return "net of commission"
    if alloc.tier == Tier.T4_REMITTANCE:
        return "net of commission (cash due, per the remittance advice)"
    if alloc.tier == Tier.T3_REFERENCE:
        return "amount due (cash basis)"
    return "proposal (lump sum, cash due)"


def build_line_export(store: Store, bank_line_id: str) -> dict:
    """Assemble the export body for one bank line. Pure read."""
    line = next(
        (l for l in store.bank_lines() if l.bank_line_id == bank_line_id), None
    )
    if line is None:
        raise LineExportError(f"no such bank line: {bank_line_id}")

    all_events = store.audit_events()
    packs = sorted(store.packs(), key=lambda p: (p.value_date, p.pack_id))
    exceptions = sorted(
        (e for e in store.exceptions() if e.bank_line_id == bank_line_id),
        key=lambda e: e.exception_id,
    )
    suspense = sorted(
        (i for i in store.suspense_items() if i.bank_line_id == bank_line_id),
        key=lambda i: i.suspense_id,
    )
    ret = store.return_for_line(bank_line_id)

    # -- provenance ---------------------------------------------------
    source: dict = {"kind": line.source, "import_batch": line.import_batch}
    if line.source == "import" and line.import_batch:
        for e in all_events:
            if e.action == "statement_imported" and f"batch {line.import_batch}" in e.detail:
                source["import"] = {
                    "source_file": e.subject,
                    "at": e.at.isoformat(),
                    "detail": e.detail,
                }
                break

    # -- the ladder, as recorded --------------------------------------
    attempts = []
    for e in all_events:
        if e.action == "line_attempts" and e.subject == bank_line_id:
            try:
                stages = json.loads(e.detail)
            except (ValueError, TypeError):
                stages = [{"stage": "recorded", "result": "unparsed",
                           "detail": e.detail}]
            attempts.append({"at": e.at.isoformat(), "actor": e.actor,
                             "stages": stages})

    # -- allocations, offsets, write-offs, by pack --------------------
    allocations: list[dict] = []
    offsets: list[dict] = []
    write_offs: list[dict] = []
    gate_history: list[dict] = []
    posted = pending = rejected = ZERO
    offsets_posted = offsets_pending = ZERO
    np_posted = np_pending = np_rejected = ZERO
    non_premium_disposition = None
    pack_ids: list[str] = []
    exc_ids = {e.exception_id for e in exceptions}
    for pack in packs:
        entries_for_line = []
        pack_alloc = pack_off = pack_wo = ZERO
        pack_edits: list[dict] = []
        for idx, entry in enumerate(pack.entries):
            if entry.allocation and entry.allocation.bank_line_id == bank_line_id:
                a = entry.allocation
                allocations.append({
                    "pack_id": pack.pack_id,
                    "pack_status": pack.status.value,
                    "entry_index": idx,
                    "policy_number": a.policy_number,
                    "installment_id": a.installment_id,
                    "amount": as_str(a.amount),
                    "currency": a.currency,
                    # The recorded conversion, all three figures:
                    # the cash amount above (in `currency`), the
                    # governed rate used, and the functional amount
                    # it produced. Null for functional-currency
                    # allocations, where the amount is the figure.
                    "fx_rate": str(a.fx_rate) if a.fx_rate is not None else None,
                    "functional_amount": (
                        as_str(a.functional_amount)
                        if a.functional_amount is not None else None),
                    "tier": a.tier.value if a.tier else None,
                    "basis": _basis(a),
                    "proposal_kind": entry.proposal_kind,
                    "proposed_by": entry.proposed_by,
                    "auto_applied": entry.auto_applied,
                    "note": a.note,
                    "edits": [
                        {
                            "at": ed.at.isoformat(), "by": ed.by,
                            "before": {"policy_number": ed.before_policy,
                                       "installment_id": ed.before_installment,
                                       "amount": as_str(ed.before_amount)},
                            "after": {"policy_number": ed.after_policy,
                                      "installment_id": ed.after_installment,
                                      "amount": as_str(ed.after_amount)},
                        }
                        for ed in entry.edits
                    ],
                })
                entries_for_line.append(idx)
                pack_alloc += a.amount
                if pack.status.value == "approved":
                    posted += a.amount
                elif pack.status.value == "proposed":
                    pending += a.amount
                else:
                    rejected += a.amount
                for ed in entry.edits:
                    pack_edits.append({"entry_index": idx, "by": ed.by,
                                       "at": ed.at.isoformat(),
                                       "before_amount": as_str(ed.before_amount),
                                       "after_amount": as_str(ed.after_amount),
                                       "before_target": f"{ed.before_policy}/{ed.before_installment}",
                                       "after_target": f"{ed.after_policy}/{ed.after_installment}"})
            elif entry.payable_offset and entry.payable_offset.bank_line_id == bank_line_id:
                o = entry.payable_offset
                offsets.append({
                    "pack_id": pack.pack_id,
                    "pack_status": pack.status.value,
                    "entry_index": idx,
                    "payable_id": o.payable_id,
                    "policy_number": o.policy_number,
                    "amount": as_str(o.amount),
                })
                entries_for_line.append(idx)
                pack_off += o.amount
                if pack.status.value == "approved":
                    offsets_posted += o.amount
                elif pack.status.value == "proposed":
                    offsets_pending += o.amount
            elif entry.non_premium and entry.non_premium.bank_line_id == bank_line_id:
                np = entry.non_premium
                non_premium_disposition = {
                    "pack_id": pack.pack_id,
                    "pack_status": pack.status.value,
                    "entry_index": idx,
                    "kind": np.kind,
                    "amount": as_str(np.amount),
                    "payer_name": np.payer_name,
                }
                entries_for_line.append(idx)
                if pack.status.value == "approved":
                    np_posted += np.amount
                elif pack.status.value == "proposed":
                    np_pending += np.amount
                else:
                    np_rejected += np.amount
            elif entry.kind == "write_off" and entry.write_off_exception_id in exc_ids:
                write_offs.append({
                    "pack_id": pack.pack_id,
                    "pack_status": pack.status.value,
                    "entry_index": idx,
                    "exception_id": entry.write_off_exception_id,
                    "policy_number": entry.write_off_policy,
                    "amount": as_str(entry.write_off_amount or ZERO),
                })
                entries_for_line.append(idx)
                pack_wo += entry.write_off_amount or ZERO
        if entries_for_line:
            pack_ids.append(pack.pack_id)
            rejection = None
            for e in all_events:
                if e.action == "pack_rejected" and e.subject == pack.pack_id:
                    rejection = {"by": e.actor, "at": e.at.isoformat(),
                                 "reason": e.detail}
            if pack.pack_id.startswith("PACK-REMATCH"):
                pack_kind = "rematch"
            elif pack.pack_id.startswith("PACK-COMB"):
                pack_kind = "combination"
            elif pack.pack_id.startswith("NPACK-"):
                pack_kind = "non_premium_disposition"
            else:
                pack_kind = "posting"
            gate_history.append({
                "pack_id": pack.pack_id,
                "kind": pack_kind,
                "value_date": pack.value_date.isoformat(),
                "assembled_by": pack.assembled_by,
                "status": pack.status.value,
                "approvals": list(pack.approvals),
                "decided_at": pack.decided_at.isoformat() if pack.decided_at else None,
                "flags": list(pack.flags),
                "entries_for_line": entries_for_line,
                "allocations_for_line": as_str(pack_alloc),
                "offsets_for_line": as_str(pack_off),
                "write_offs_for_line": as_str(pack_wo),
                "edits": pack_edits,
                "rejection": rejection,
            })

    # -- exceptions, with their handling acts --------------------------
    exc_views = []
    for e in exceptions:
        view = _json(e)
        view["acts"] = [
            {"at": ev.at.isoformat(), "actor": ev.actor, "action": ev.action,
             "detail": ev.detail}
            for ev in all_events if ev.subject == e.exception_id
        ]
        exc_views.append(view)

    # -- suspense and the re-match attempts on it ----------------------
    susp_ids = {i.suspense_id for i in suspense}
    rematch_attempts = [
        {"at": e.at.isoformat(), "actor": e.actor, "suspense_id": e.subject,
         "detail": e.detail}
        for e in all_events
        if e.action == "suspense_rematch_attempt" and e.subject in susp_ids
    ]
    suspense_open = sum((i.amount for i in suspense if i.status == "open"), ZERO)

    # -- returns and re-sends, both ways -------------------------------
    resent_as = sorted(
        l.bank_line_id for l in store.bank_lines() if l.resend_of == bank_line_id
    )
    original_return = None
    if line.resend_of:
        original = store.return_for_line(line.resend_of)
        original_return = original.return_id if original else None

    # -- final state ----------------------------------------------------
    returned_amount = ret.amount if ret else ZERO
    reversed_amount = posted if ret else ZERO
    if ret is not None:
        state = "returned"
        state_detail = (
            f"The bank took the full line back ({ret.reason.value}, recorded "
            f"{ret.return_date} by {ret.recorded_by}); its application stands "
            "reversed and its suspense released."
        )
    elif non_premium_disposition is not None:
        state = "classified non-premium"
        state_detail = (
            f"Classified {non_premium_disposition['kind']} in code before the "
            f"tiers: not premium cash. It stands in disposition pack "
            f"{non_premium_disposition['pack_id']} ({non_premium_disposition['pack_status']})"
            + (" — posted to the non-premium ledger view."
               if non_premium_disposition["pack_status"] == "approved" else
               " — at the gate, or decided; it never enters premium suspense.")
        )
    elif posted > ZERO and suspense_open == ZERO and pending == ZERO:
        state = "applied"
        state_detail = "Settled under approved pack(s); nothing outstanding."
    elif posted > ZERO and suspense_open > ZERO:
        state = "partly applied"
        state_detail = "Part settled under approved pack(s); the remainder sits in suspense."
    elif posted > ZERO:
        state = "applied, with more at the gate"
        state_detail = "Part settled under approved pack(s); further allocations await a gate decision."
    elif pending > ZERO and suspense_open > ZERO:
        state = "proposed at the gate; remainder in suspense"
        state_detail = "Allocations are assembled in a proposed pack; the rest of the line sits in suspense."
    elif pending > ZERO:
        state = "proposed at the gate"
        state_detail = "Allocations are assembled in a proposed pack awaiting a named approver."
    elif suspense_open > ZERO:
        state = "in suspense"
        state_detail = "Unapplied cash held in suspense."
    elif rejected > ZERO:
        state = "on a rejected pack"
        state_detail = "Its allocations stand on a rejected pack; the cash is unapplied until the line is re-worked."
    else:
        state = "received, not yet worked"
        state_detail = "On the books; no day run has worked this line yet."

    # -- footing ---------------------------------------------------------
    accounted = (
        posted - reversed_amount + pending
        + offsets_posted + offsets_pending
        + np_posted + np_pending
        + suspense_open + returned_amount
    )
    foots = accounted == line.amount
    terms = [
        f"allocations posted {as_str(posted)}",
        f"reversed by return {as_str(reversed_amount)}" if ret else None,
        f"allocations at the gate {as_str(pending)}" if pending > ZERO else None,
        f"payable offsets posted {as_str(offsets_posted)}" if offsets_posted > ZERO else None,
        f"payable offsets at the gate {as_str(offsets_pending)}" if offsets_pending > ZERO else None,
        f"non-premium disposition posted {as_str(np_posted)}" if np_posted > ZERO else None,
        f"non-premium disposition at the gate {as_str(np_pending)}" if np_pending > ZERO else None,
        f"suspense open {as_str(suspense_open)}" if suspense_open > ZERO else None,
        f"returned to the payer {as_str(returned_amount)}" if ret else None,
    ]
    footing_note = (
        "The line FOOTS: " + " + ".join(t for t in terms if t)
        + f" = the bank amount {as_str(line.amount)}, to the cent."
        if foots else
        f"THE LINE DOES NOT FOOT inside this export: the terms above account "
        f"for {as_str(accounted)} of the bank amount {as_str(line.amount)} "
        f"(difference {as_str(accounted - line.amount)})."
        + (f" {as_str(rejected)} of allocations stand on rejected pack(s) and "
           "settle nothing until the line is re-worked." if rejected > ZERO else "")
        + (f" Its non-premium disposition of {as_str(np_rejected)} stands on a "
           "rejected pack and posts nothing until the line is re-presented."
           if np_rejected > ZERO else "")
    )

    # -- the audit trail --------------------------------------------------
    subjects = {bank_line_id, *pack_ids, *exc_ids, *susp_ids}
    trail = [
        {"at": e.at.isoformat(), "actor": e.actor, "action": e.action,
         "subject": e.subject, "detail": e.detail}
        for e in all_events
        if e.subject in subjects
        and e.action not in ("line_attempts", "suspense_rematch_attempt")
    ]

    return {
        "bank_line_id": bank_line_id,
        "line": _json(line),
        "source": source,
        "attempts_recorded": bool(attempts),
        "attempts": attempts,
        "allocations": allocations,
        "payable_offsets": offsets,
        "non_premium": non_premium_disposition,
        "write_offs": write_offs,
        "exceptions": exc_views,
        "suspense": [_json(i) for i in suspense],
        "rematch_attempts": rematch_attempts,
        "gate_history": gate_history,
        "return": _json(ret) if ret else None,
        "resend": {
            "resend_of": line.resend_of,
            "resend_of_return_id": original_return,
            "resent_as": resent_as,
        },
        "final_state": {
            "state": state,
            "detail": state_detail,
            "allocations_posted": as_str(posted),
            "suspense_open": as_str(suspense_open),
            "returned": as_str(returned_amount),
        },
        "footing": {
            "line_amount": as_str(line.amount),
            "allocations_posted": as_str(posted),
            "allocations_reversed_by_return": as_str(reversed_amount),
            "allocations_at_gate": as_str(pending),
            "allocations_on_rejected_packs": as_str(rejected),
            "payable_offsets_posted": as_str(offsets_posted),
            "payable_offsets_at_gate": as_str(offsets_pending),
            "suspense_open": as_str(suspense_open),
            "returned_amount": as_str(returned_amount),
            "accounted": as_str(accounted),
            "foots": foots,
            "note": footing_note,
        },
        "audit_trail": trail,
    }


def line_export(store: Store, bank_line_id: str) -> dict:
    """The exportable artifact: the body and its SHA-256 digest."""
    body = build_line_export(store, bank_line_id)
    return {
        "bank_line_id": bank_line_id,
        "sha256": pack_digest(body),
        "export": body,
    }


def render_line_export_text(export: dict) -> str:
    body = export["export"]
    line = body["line"]
    out = [
        f"PER-LINE AUDIT EXPORT — {body['bank_line_id']}",
        f"SHA-256 {export['sha256']}",
        "",
        "THE LINE AS RECEIVED",
        f"  Value date {line['value_date']} · amount {line['amount']} · "
        f"payer {line['payer_name']}",
        f"  Reference: {line['reference'] or '—'}",
        f"  Narrative: {line['narrative'] or '—'}",
    ]
    src = body["source"]
    if src["kind"] == "import":
        out.append(f"  Source: statement import, batch {src['import_batch']}"
                   + (f" (file {src['import']['source_file']}, {src['import']['at']})"
                      if src.get("import") else ""))
    elif src["kind"] == "dataset":
        out.append("  Source: the synthetic Northgate dataset")
    else:
        out.append("  Source: manual (added directly; no ingestion batch)")
    out += ["", "TIERS ATTEMPTED, IN ORDER"]
    if not body["attempts_recorded"]:
        out.append(
            "  Tier attempts were not recorded for this line — it was "
            "matched before attempt recording. The outcome below is "
            "the record; no ladder is invented for it.")
    for working in body["attempts"]:
        out.append(f"  Working at {working['at']} (by {working['actor']}):")
        for s in working["stages"]:
            out.append(f"    {s['stage']:<20} {s['result']:<24} {s['detail']}")
    out += ["", f"ALLOCATIONS ({len(body['allocations'])})"]
    for a in body["allocations"]:
        out.append(
            f"  {a['pack_id']} [{a['pack_status']}] {a['policy_number']}/"
            f"{a['installment_id']} {a['amount']:>10} · tier "
            f"{a['tier'] or 'proposal'} · basis {a['basis']}"
            + (f" · proposed by {a['proposed_by']} ({a['proposal_kind']})"
               if a["proposal_kind"] else "")
            + (" · auto-appliable" if a["auto_applied"] else "")
        )
        if a["note"]:
            out.append(f"      note: {a['note']}")
        for ed in a["edits"]:
            out.append(
                f"      edited by {ed['by']} at {ed['at']}: "
                f"{ed['before']['policy_number']}/{ed['before']['installment_id']} "
                f"{ed['before']['amount']} → {ed['after']['policy_number']}/"
                f"{ed['after']['installment_id']} {ed['after']['amount']}")
    for o in body["payable_offsets"]:
        out.append(
            f"  {o['pack_id']} [{o['pack_status']}] payable offset "
            f"{o['payable_id']} ({o['policy_number']}) {o['amount']:>10}")
    for w in body["write_offs"]:
        out.append(
            f"  {w['pack_id']} [{w['pack_status']}] write-off "
            f"{w['exception_id']} ({w['policy_number']}) {w['amount']:>10}")
    out += ["", f"EXCEPTIONS ({len(body['exceptions'])})"]
    for e in body["exceptions"]:
        out.append(
            f"  {e['code']:<22} {e['exception_id']} {e['amount']:>10} "
            f"[{e['status']}] first seen {e['first_seen']}")
        out.append(f"      {e['detail']}")
        if e.get("assignee"):
            out.append(f"      assigned to {e['assignee']}")
        for n in e.get("handling_notes") or []:
            out.append(f"      note {n['at']} · {n['by']}: {n['text']}")
        for act in e["acts"]:
            out.append(f"      act {act['at']} · {act['actor']} · {act['action']}: {act['detail']}")
    if body["suspense"]:
        out += ["", "SUSPENSE"]
        for i in body["suspense"]:
            out.append(
                f"  {i['suspense_id']} {i['amount']:>10} · received "
                f"{i['received_date']} · {i['reason']} · [{i['status']}]"
                + (f" · resolved {i['resolved_on']}" if i.get("resolved_on") else ""))
    if body["rematch_attempts"]:
        out += ["", "RE-MATCH ATTEMPTS"]
        for a in body["rematch_attempts"]:
            out.append(f"  {a['at']} · {a['actor']} · {a['suspense_id']}: {a['detail']}")
    out += ["", "GATE HISTORY"]
    for g in body["gate_history"]:
        out.append(
            f"  {g['pack_id']} ({g['kind']}) · value {g['value_date']} · "
            f"assembled by {g['assembled_by']} · [{g['status']}]"
            + (f" · approved by {', '.join(g['approvals'])}" if g["approvals"] else "")
            + (f" · decided {g['decided_at']}" if g["decided_at"] else "")
            + (f" · flags: {', '.join(g['flags'])}" if g["flags"] else ""))
        for ed in g["edits"]:
            out.append(
                f"      entry {ed['entry_index']} edited by {ed['by']} at "
                f"{ed['at']}: {ed['before_target']} {ed['before_amount']} → "
                f"{ed['after_target']} {ed['after_amount']}")
        if g["rejection"]:
            out.append(
                f"      rejected by {g['rejection']['by']} at "
                f"{g['rejection']['at']}: {g['rejection']['reason']}")
    if body["return"]:
        r = body["return"]
        out += ["", "RETURN",
                f"  {r['return_id']} · {r['reason']} · {r['amount']} · returned "
                f"{r['return_date']} · recorded by {r['recorded_by']}",
                f"  {r['detail']}"]
    resend = body["resend"]
    if resend["resend_of"] or resend["resent_as"]:
        out += ["", "RE-SEND LINKS"]
        if resend["resend_of"]:
            out.append(f"  This line re-sends {resend['resend_of']}"
                       + (f" (return {resend['resend_of_return_id']})"
                          if resend["resend_of_return_id"] else ""))
        for rid in resend["resent_as"]:
            out.append(f"  Re-sent as {rid} after this line was returned")
    f = body["footing"]
    out += [
        "",
        "FOOTING",
        f"  Bank amount:                 {f['line_amount']:>10}",
        f"  Allocations posted:          {f['allocations_posted']:>10}",
        f"  Reversed by return:          {f['allocations_reversed_by_return']:>10}",
        f"  Allocations at the gate:     {f['allocations_at_gate']:>10}",
        f"  On rejected packs:           {f['allocations_on_rejected_packs']:>10}",
        f"  Payable offsets posted:      {f['payable_offsets_posted']:>10}",
        f"  Payable offsets at the gate: {f['payable_offsets_at_gate']:>10}",
        f"  Suspense open:               {f['suspense_open']:>10}",
        f"  Returned to the payer:       {f['returned_amount']:>10}",
        f"  Accounted:                   {f['accounted']:>10}",
        f"  {f['note']}",
        "",
        f"FINAL STATE: {body['final_state']['state']} — {body['final_state']['detail']}",
        "",
        "AUDIT TRAIL",
    ]
    for e in body["audit_trail"]:
        out.append(f"  {e['at']} · {e['actor']} · {e['action']} · {e['subject']}: {e['detail']}")
    out += [
        "",
        "This export is a read model over the books: building it changed "
        "nothing, and it regenerates byte-identically from unchanged books.",
    ]
    return "\n".join(out) + "\n"
