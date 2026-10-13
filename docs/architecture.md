# Architecture — Insurance Cash Reconciliation Agent

How the blueprint is put together, and why. The README is the tour;
this document is the engineering record.

## Shape

```
                 ┌──────────────┐   documents   ┌────────────────┐
 remittance ───▶ │ object store │ ─────────────▶│ document screen │
 advices         │ (S3)         │               │ + embeddings    │
                 └──────────────┘               └───────┬────────┘
                                                        │ fenced text
 bank lines ──┐                                         ▼
              ├──────────────────────────────▶  ┌──────────────────┐
 receivables ─┘                                 │ reader (model    │
 (policies,                                     │ port, validated) │
  installments,                                 └───────┬──────────┘
  broker rates)                                         │ proposed lines
                                                        ▼
                                              ┌──────────────────┐
                                              │ tiered matcher   │
                                              │ (code, T1–T5)    │
                                              └───┬──────┬───────┘
                                     allocations  │      │ exceptions
                                                  ▼      ▼
                                        ┌────────────┐ ┌──────────────┐
                                        │ posting    │ │ exception    │
                                        │ pack       │ │ queue +      │
                                        └─────┬──────┘ │ suspense     │
                                              │        └──────────────┘
                                       HUMAN GATE (named approver)
                                              │
                                              ▼
                              posting file (CSV) · receivables updated
                              daily reconciliation report (footing control)
```

The pipeline is a LangGraph graph — `screen → read → match → persist
→ assemble → sweep → report` — run once per value date. It stops at
the gate by construction: approval is a separate act through the API
or console, by a person the graph cannot impersonate.

## Decisions that matter

**Matching is code; the model proposes.** Tier order, netting math,
duplicate detection, allocation validation and report footing are
deterministic functions with unit tests. The model has exactly three
jobs — reading advices, proposing lump-sum allocations, drafting
broker notes — and each output crosses a code validator before it can
influence anything: citations must be verbatim document text;
proposals must name real open items and sum to the wire to the cent.

**Cash-due basis.** Broker business is billed gross and settled net.
All matching therefore works on a per-installment *cash due*: the
billed amount for direct business, `billed − commission(billed, rate)`
for broker business, with the commission rounded half-up to cents —
the single rounding point in the system. When an approved allocation
is applied to a receivable, broker cash settles its gross equivalent
(`cash / (1 − rate)`), and cash at or above the remaining net due
settles the installment in full. This keeps "what the bank sent" and
"what the ledger earned" as two honest numbers, each traceable.

**Reference-led scope.** A payment carrying a policy reference is read
against the next open installment unless it exactly matches a wider
scope — the full open balance, or an exact run of consecutive
installments. Overpaying the next installment parks the excess in
suspense as OVERPAID rather than silently consuming future
installments nobody billed yet.

**Installment-aware allocation.** Each installment is its own open
item (sequence, due date, own balance), and `open_installments()`
guarantees sequence order — the allocation order is a property of
the record, not of storage order. The structured reference is the
only instruction channel: a reference naming an installment
(`NSP-1003-I3`) routes the cash to that installment first, with any
remainder flowing to the oldest open installment after it and the
flow written onto the allocations and the outcome notes. Free text
is never an instruction: a narrative indicating a later installment
leaves the allocation exactly where the rule puts it (oldest first)
and adds an INTENT_DIFFERS exception record — amount applied
elsewhere, in words — so the desk sees the difference without the
money being held hostage to it. A named installment that is already
settled takes the same path: fall back to the rule, flag the intent.

**Netting mismatch vs partial.** Both are "less than expected". The
distinguishing test is the implied commission rate: if
`(gross − received) / gross` is a clean rate (to a tenth of a percent)
that is not the contracted rate, the broker deducted *a* rate — the
wrong one — and the line is a NETTING_MISMATCH with the cash held
unapplied. Otherwise it is a part-payment: allocated, with the
shortfall a PARTIAL exception and the balance still collectable.

**Every cent accounted, asserted.** A matcher outcome must satisfy
`allocations + payable offsets + suspense == bank amount`; the
matcher asserts it per line, and the daily report re-derives the
identity over the whole day. A day that does not foot is reported
NOT reconciled, in words.

**Broker rates are temporal.** Commission used to be one number on
the broker record, read wherever cash-due was computed — a design
that cannot say what rate a September wire should have netted at
once the rate changes in October. `Broker.rate_history` is a list
of dated periods and `rate_on(as_of)` is the single way any rate is
read: the matcher prices a line at the rate in force on its value
date (netting, the mismatch test, the named-installment path, the
lump-sum candidates), the gate grosses up at the rate in force on
the pack's value date, and the returns replay uses the same
per-pack date — so a rate change reprices nothing that already
happened, and every computation names the period it priced. The
headline `commission_rate` remains as the current rate (and the
answer for a record with no history), set to the newest period's
rate whenever a history is booked.

**A lump sum never crosses brokers.** Candidates for the proposers
are built from the paying broker's open items only, and both
readers validate proposals against that list — but the rule also
stands in the matcher itself: a proposal naming an item whose
policy belongs to another broker is refused whole, whoever produced
it, and the wire falls through to unidentified. Two desks' money is
never mixed by a settlement, and the refusal does not depend on
the proposer's good behaviour.

**Return premiums: a second ledger, the same gate.** Cancellation
(`cancellations.py`) is a governed recording act with named
refusals — the policy must be on the books, not already cancelled,
and carrying term dates, because the computation is pro-rata by
days and a pro-rata with nothing to stand on is a guess. Earned is
billed premium × elapsed days ÷ term days (half-up at the cent —
the computation is printed on the record); paid-to-date is the
installments' applied sum on the billed basis; the return is the
difference when positive. The payable it opens is a record with an
append-only chain (opened / netted / paid), owed to the insured or
the broker of record. Incoming cash for the cancelled policy meets
the payable in the matcher, before the cancelled-policy exception:
the offset is a pack entry like any other, so it lands only at
approval — the gate stays the only writer, of payables too — and a
per-run shadow ledger stops two wires in one run netting more than
the payable holds. Payouts (`payouts.py`) reuse the gate's exact
discipline and its single-approver limit: self-approval refused, a
second approver above the limit, and approval re-validates each
payable's standing amount, refusing the pack whole if netting moved
one since assembly. The controls keep the ledgers apart on purpose:
the day identity gains the netted term (cash that arrived and was
accounted), while the close pack rolls payables on their own
identity — owed money is not held cash, and an identity that mixed
them would foot by lying.

**The Today view is a read model, not a state.** The day-open
checklist (`today.py`) derives six step states from the same
records the tabs render — bank lines present, the day's pack,
the open exception queue, proposed packs anywhere, re-match
attempts stamped with the day they ran for, the day's cancellation
and return records. Nothing is stored per day: a step cannot drift
from the act it reports, because it *is* the act, re-read.

**Documents are hostile until screened.** The screen runs before any
reader — model or human-facing summary — sees a remittance. Flagged
lines are replaced with a fence marker, the event is audited, and the
rest of the document is read normally. Embeddings (a deterministic
hashing embedder offline; pgvector storage in production) catch the
re-sent advice: flagged as a near-duplicate, not dropped.

**The gate is the only writer.** Receivables change in exactly one
place — pack approval in `gate.py`. That is what makes the audit
claim true: every applied cent traces to bank line + allocation +
pack + named approver. Write-offs are pack entries, not edits; a
write-off above the single-approver limit cannot complete without a
second, different approver, and both names land on the pack. The one
sanctioned counter-move is a recorded return (below): it reverses a
gate application, deriving its amounts from the gate's own record —
it introduces no second way of deciding what cash means.

**Returns reverse; they never edit.** Applied cash comes back —
NSF, a recall, a dishonoured cheque — and the tempting
implementation is to delete the application and pretend the money
was never there. `returns.py` does the opposite. The reversal
amounts are derived by replaying the approved packs, in approval
order, through `gate._apply` over pristine receivables: the same
arithmetic that posted the cash, so the delta attributed to the
returned line is exactly the delta the gate posted (the broker
gross-up, with its capped settle-in-full branch, is where a naive
"subtract the allocation" would quietly lose cents). The packs
stand byte for byte and gain a RETURN_AFFECTED flag; the return is
its own record and appears as a reversing entry on its day's
report and in the close pack, whose suspense reconstruction gives
returned cash its own roll term — an item's position goes to zero
on the return date, and `returned_from_suspense` is what the roll
subtracts, so the month foots with returns inside the identity.
The duplicate check had to learn the same lesson: a returned line
leaves `settled_line_ids` semantics unchanged (its allocations are
still on record), so the matcher takes an explicit
`returned_line_ids` set and skips those priors — the re-send is a
new bank line, fingerprinted like any other, and the link back to
the returned original is written onto its record at persist time.
Part-returns are refused rather than modelled: a fractional notion
of "applied" would infect every control in the system, and the
refusal states the scope in words.

**The workbench handles; it does not dispose.** The exception
queue is worked daily, and the failure mode to avoid is a second,
softer set of disposition buttons growing next to the governed
ones. `workbench.py` therefore owns exactly two acts — assignment
to a rostered name, and an append-only handling note — both
audited, both provably inert against the exception's amount, code
and status. The view is a fresh read model whose counts are taken
over the whole open queue before filters apply, so the counts foot
to the queue totals by construction rather than by agreement.

**Proposals are graded by the gate's record.** A quality number
for the model is only worth having if nobody gets to assert it.
Pack entries carry their provenance from assembly (kind + backend)
and, after this round, their edits — each edit a record with the
original, the replacement, and the approver who made it, admitted
only if the affected bank line still foots within the pack
(`gate.edit_pack_entries`). `report.proposal_quality` then derives
outcomes mechanically: pack rejected → REJECTED; approved with
edits → ACCEPTED_WITH_EDITS (the edited value is the amount delta,
or the full amount when money was redirected); approved untouched
→ ACCEPTED_AS_PROPOSED. There is deliberately no per-person
breakdown: the report's subject is the model's work, and the
editor's name stays where it belongs — on the entry, in the audit
trail. Provenance is stamped from this round forward; packs
assembled before it carry no kind and are simply not graded.

**Suspense is re-worked, not just aged.** A day run settles cash
against the books as they stood on its value date; explanations that
arrive later — a corrected advice, a policy booked after its wire —
would otherwise wait for a human to notice them. The re-match pass
(`rematch.py`) is the explicit counter-move: an operator-invoked run
over open suspense items whose bank line is wholly unapplied,
re-entering the same matcher with a context built from current
state. Late documents are prepared on the spot by the same
screen-and-read path the day graph uses, and the pairing rule widens
to claim them (same broker, and the advice's day or its total
matching the wire; the broker's latest advice wins, and T4's
arithmetic validation still decides). Every safeguard is inherited
by construction — per-line footing, duplicate checks, allocations
assembled into a `PACK-REMATCH-<date>` gate pack — and each attempt
is audited against the item with the tiers run and the outcome, so
"we tried on the 8th and the advice still did not foot" is a
record, not a memory.

**Brokers are scored on paperwork, not people.** The scorecard
(`report.broker_scorecard`) is a read model over the same records
the daily reports use, with the same line-to-broker attribution —
so its money columns foot to the sum of the daily by-broker splits
by construction, an identity the eval gate checks as a control. It
answers the questions a desk lead actually asks of a broker
relationship: how much arrived, how much is still unapplied and for
how long on average, what share of netting-tier settlements came
through without an exception, and what the broker's rate mistakes
cost in held cash. The drafted query notes hang off the row they
belong to. Nothing in it ranks people or contacts anyone.

**The close pack is a read model over the suspense ledger.** The
month-end pack (`close_pack.py`) rolls suspense, not cash in
general: opening + received into suspense − applied from suspense =
closing. To make that checkable rather than asserted, each item's
position is reconstructed from the records — the amount that
entered suspense when its line was worked (bank amount less the
day packs' allocations for the line) less what re-match packs have
released since, by pack value date. The roll closing and the
reconstructed closing are two computations of one number; a
disagreement, or an item whose bank line is missing, makes the pack
say NOT FOOTED and name the difference and the item. Approved
write-offs are a control line of their own: in this system a
write-off disposes a receivable delta (a partial shortfall, a
netting delta), never suspense cash, so netting it into the
suspense identity would make the roll lie. The pack body is
canonical and wall-clock-free; its SHA-256 digest covers the body,
and the JSON export and text rendering are two views of it.
Assembling a pack mutates nothing — closing a month is a report,
not a lock, and there is deliberately no "closed period" state
anywhere in the stores.

**Statements arrive as files, imported whole or not at all.** The
importer (`statement.py`) recognises a generic bank CSV by a small
column-alias config — debit/credit pair or one signed amount — and
emits the same `BankLine` records the tiers consume, through the
existing store port (no new persistence, so the conformance battery
is untouched). Parsing completes before the first store write, and
trailer controls (CONTROL TOTAL / CONTROL COUNT) are checked
against the parsed credits and row count, so a refused import
leaves the books exactly as they were. Dedupe is structural: the
imported line's id is a digest of the dedupe key (bank reference +
amount + value date), so a re-import meets the same ids in the
store and reports them as already-seen instead of storing twins.
The statement's own reference column reaches `BankLine.reference`
only when it carries a policy number — otherwise it would break the
matcher's "no reference" signal for remittance-led lines, and the
policy reference is found in the description text as usual.

## Stores

- **PostgreSQL** (production): one table per record type, payloads as
  JSONB with amounts as decimal strings, key columns beside them for
  the queries the desk runs. Numbered SQL migrations in `migrations/`
  are applied by the store at startup; `0002` adds the pgvector
  embedding table, `0003` the payment-returns table, `0004` the
  return-premium tables (cancellations, payables, payout packs).
  LangGraph
  checkpoints use the Postgres saver in the same database when
  `DATABASE_URL` is set. The allocations table is an index of pack
  entries; when the gate edits a pack, `replace_allocations` rewrites
  that pack's rows so the index never describes a pack that no
  longer stands.
- **SQLite** (the local store): one file, `./data/cashrecon.db` by
  default (`SQLITE_PATH` moves it) — the local run needs no database
  server at all. The SQLite-dialect migrations in
  `migrations/sqlite/` mirror the Postgres set table for table;
  payloads are JSON text, money key columns are TEXT decimal strings
  (never REAL, so SQLite's type affinity never touches a cent), and
  the LangGraph checkpointer is the SQLite saver writing into the
  same file. Document embeddings are stored as JSON arrays and the
  near-duplicate cosine is computed in code over the stored vectors:
  Postgres leans on pgvector's index, SQLite does the arithmetic
  itself — same embedder, same threshold, same answers, different
  machinery. Which store runs is one decision in
  `store.store_profile()`: `DATABASE_URL` set means Postgres,
  otherwise SQLite. A behavioural conformance battery runs the same
  assertions against all three stores, so the implementations
  cannot silently drift apart.
- **S3-compatible object storage**: remittance documents, keyed
  `remittances/<id>.txt`. The client is a small SigV4 signer over the
  standard library. In compose, SeaweedFS's S3 gateway serves the
  `minio` hostname; the bucket is created by a one-shot unsigned PUT
  and re-checked by the API at startup.
- **In-memory doubles** implement the same protocol for tests and
  the eval gate. They are test doubles only.

## Configuration

Policy thresholds live in `config.py` and the environment:
`AUTO_APPLY_THRESHOLD` (500.00), `WRITE_OFF_SINGLE_LIMIT` (250.00),
`STALE_SUSPENSE_DAYS` (30), `NETTING_TOLERANCE` (0.01),
`DUPLICATE_WINDOW_DAYS` (5). Provider selection is `LLM_BACKEND`
(`auto` default) plus the provider variables in `.env.example`.
Store selection is `DATABASE_URL` (PostgreSQL) or `SQLITE_PATH`
(SQLite file, default `data/cashrecon.db`) — an env change only.

## What v1 deliberately does not do

No live bank feeds (statements arrive as files and are imported;
nothing polls a bank), no general-ledger integration
(the approved pack's CSV is the hand-off), no payment execution, no
claims or reinsurance cash — premium cash application for one
carrier. Auto-application exists only as a *mark* on small exact
matches; a deployment that wants true straight-through posting for
that slice approves those packs in bulk, with the report still
naming every line. Part-returns are not modelled (a bank returning
part of a line is refused at recording, in words), and proposal
provenance exists only on packs assembled since the quality report
landed — older packs are ungraded, not backfilled.
