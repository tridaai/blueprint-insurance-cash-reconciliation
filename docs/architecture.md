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

**Currency is a denomination, not a conversion problem.** Every
money record carries the ISO currency its amount is denominated in
(bank lines, receivables and installments, allocations, suspense
items, exceptions, returns; a key column in both store dialects,
migration 0005, beside the payload). The matcher is currency-first:
a line is worked only against open items in its own currency —
tiers, remittance lines, attribution, lump-sum candidates, and
combination pairs alike — because a converted figure that foots is
still the wrong settlement. Where the payer has nothing open in
the line's currency, the outcome is the `CURRENCY_NO_OPEN_ITEMS`
exception and suspense in the line's own denomination; the model
is never asked, because there is no judgment to make. Conversion
exists for exactly two purposes, both in `fx.py` over the governed
table in `config.py` (`FX_RATE_HISTORY`, dated periods in the
broker-rate mould, `rate_on` the single read): *recording* — when
an allocation is created, the functional amount is computed at the
rate in force on the bank line's value date, rounded half-up at
that one point, and stored on the allocation with the rate, where
it is never recomputed (a gate edit re-derives it at the recorded
rate; a return's reversal replays billed terms in the line's own
currency, so the recorded conversion stands untouched even after
the rate moves) — and *presentation* — day reports and close packs
translate positions into the functional currency at named rates,
labelled as translations. Controls never run on translated
figures: the day footing and the close pack's suspense identity
are computed per currency, in the currency, and the close pack's
headline roll is simply the functional currency's own roll.
Suspense aging, the scorecard, and the trend watch likewise keep
per-currency figures and compute no blended rates.

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
returned cash its own roll term — an item's position falls by the
return's recorded suspense share on the return date (the whole
remaining position, for a full return), and
`returned_from_suspense` is what the roll subtracts, so the month
foots with returns inside the identity.
The duplicate check had to learn the same lesson: a returned line
leaves `settled_line_ids` semantics unchanged (its allocations are
still on record), so the matcher takes an explicit
`returned_line_ids` set and skips those priors — the re-send is a
new bank line, fingerprinted like any other, and the link back to
the returned original is written onto its record at persist time.

Part-returns are modelled, on the same exactness discipline —
they were refused in earlier rounds, on the argument that a
fractional notion of "applied" would infect every control in the
system. The unwind record is what answers that argument: a
part-return never leaves a fraction behind, because it records,
per allocation, the cash it took and the gross it re-opened, so
what still stands afterwards is a sum over records, not a state
someone estimated. The returned amount unwinds in one pinned
order — the line's open suspense first (cash that settled
nothing), then allocations in the reverse of application order —
each allocation re-opening by its replayed delta: whole when the
return takes all of its remaining cash, pro-rata to the cent
(half-up, at the recorded rates and conversions — never
re-priced) when it takes part, with the residual absorbed by
whichever return completes the allocation, so successive
part-returns sum to the replayed delta exactly. A line may be
returned repeatedly until its returns reach the line amount; a
return past it is refused in words, as is a part-return that
would have to disturb cash netted against a return-premium
payable (the whole standing balance remains returnable, as a
full return always was). Only a FULLY returned line joins
`returned_line_ids`: a partly returned line still stands partly
applied, so its fingerprint is not reusable — a same-amount
arrival against it is a duplicate, not a re-send. The reports
carry part-returns as distinct figures (count and total beside
the returns totals, per currency as with everything since the
currency round), and the per-line audit export names the state
"partly returned" and foots it: posted − reversed + at-gate +
offsets + suspense open + returned = the bank amount, with
"reversed" read from the returns' own unwind records.

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

**Attempts are recorded, not reconstructed.** An auditor asking
"what did you try on this line, in what order?" cannot be answered
from outcomes alone — the outcome keeps only the tier that
settled. So the matcher records its ladder as it climbs it: every
stage attempted, in true execution order, with its result and the
reason, persisted per line as a `line_attempts` audit event (the
re-match pass records its own workings the same way, under the
re-match actor). Lines also carry their provenance on the record
itself — `source` (dataset, statement import, or manual) and, for
imports, the batch id, a digest of the statement text — instead of
leaving origin to be inferred from an id prefix. Lines worked
before recording landed have no ladder, and their exports say so
rather than wearing an invented one.

**The per-line audit export foots inside itself.** The export
(`line_export.py`) is a read model over the line's whole chain —
the record as received, the recorded attempts, allocations with
their basis and gate edits, exceptions with their handling acts,
re-match attempts, gate history (assembler, edits with
before/after, approvals, rejection reasons), returns and
re-sends linked both ways, and a final-state verdict. Its footing
block states the identity in its own terms — posted allocations,
less what a return reversed, plus allocations still at the gate,
payable offsets, open suspense, and the returned amount, against
the line amount — and either confirms the line foots or names
the difference; rejected-pack allocations are reported as their
own term, never silently inside or outside the identity. The
body is canonical and wall-clock-free under the close pack's
digest discipline (SHA-256 over the sorted, compact body;
generated-at stays outside it), and building an export writes
nothing — a test pins the books identical before and after.

**The trend watch names deteriorations only.** The trend
(`trends.py`) re-aggregates the day reports' and close packs'
own records by month — bank and allocated values are the sums
of the days', suspense received is the close pack's
reconstruction — so its figures foot to the reports they
summarise by construction, and the eval gate pins both
synthetic months against the live reports. The watch on top is
a pure function of the month rows and the tolerances in
`config.WatchPolicy`: an auto-rate drop beyond the line, an
exception code's share of bank value doubling (with a floor, so
a doubling from nearly nothing is not a finding), dwell rising
beyond the line. Anything else is reported as *within
tolerance*, explicitly, with the movement figures — a watch
that only speaks when it alarms teaches its readers to treat
silence as health, which is the one thing silence cannot prove.

**Webhooks are notifications, never control flow.** Gate acts
emit small events (`webhooks.py`) to `CASH_RECON_WEBHOOK_URL`
— kind, id, actor, totals, nothing internal — through a
bounded-timeout standard-library POST with exactly one retry.
A failed delivery is audited as `webhook_failed` with the error
class and nothing more: the failure is on the record, and the
act it reported stands regardless. Readiness reports the
webhook state (configured or disabled) so an operator can tell
"no receiver" from "receiver down" without reading the trail.

## Stores

- **PostgreSQL** (production): one table per record type, payloads as
  JSONB with amounts as decimal strings, key columns beside them for
  the queries the desk runs. Numbered SQL migrations in `migrations/`
  are applied by the store at startup; `0002` adds the pgvector
  embedding table, `0003` the payment-returns table, `0004` the
  return-premium tables (cancellations, payables, payout packs),
  `0005` the currency columns and the FX story, and `0006` the
  day-claims table the run lifecycle stands on.
  The embedding table's `vector(384)` is a contract every store
  enforces in code at `set_embedding` — `EmbeddingDimensionError`,
  naming the expected dimension — so the JSON-backed stores refuse
  a wrong-dimension vector with the same deliberate error instead
  of tolerating what the schema would reject, and the conformance
  battery's fixture is a real 384-dimension vector on every leg.
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

The production profile is provable on demand rather than by
inspection: `make prod-smoke` (`scripts/prod_smoke.py`) boots the
API against live PostgreSQL + S3, seeds a fresh database, runs and
approves a day, compares close-pack digests across regeneration,
and round-trips a document through the object store — refusing to
run at all against the local profiles it is not proving.

## Configuration

Policy thresholds live in `config.py` and the environment:
`AUTO_APPLY_THRESHOLD` (500.00), `WRITE_OFF_SINGLE_LIMIT` (250.00),
`STALE_SUSPENSE_DAYS` (30), `NETTING_TOLERANCE` (0.01),
`DUPLICATE_WINDOW_DAYS` (5). The trend watch's tolerances live in
the same module (`WatchPolicy`): `WATCH_AUTO_RATE_DROP_POINTS`
(0.05), `WATCH_EXCEPTION_SHARE_DOUBLING_FACTOR` (2),
`WATCH_EXCEPTION_SHARE_FLOOR` (0.01),
`WATCH_SUSPENSE_DWELL_RISE_DAYS` (2). Gate webhooks are one
variable: `CASH_RECON_WEBHOOK_URL` (unset disables them).
Provider selection is `LLM_BACKEND`
(`auto` default) plus the provider variables in `.env.example`.
Store selection is `DATABASE_URL` (PostgreSQL) or `SQLITE_PATH`
(SQLite file, default `data/cashrecon.db`) — an env change only.
Checkpoint retention is policy in the same module:
`CHECKPOINT_KEEP_PER_THREAD` (50) and `CHECKPOINT_MAX_AGE_DAYS`
(30), with `CHECKPOINT_PRUNE_ON_STARTUP` as the opt-in startup
hook (unset = off).

## Run lifecycle: a day runs once

The day-run graph writes as it goes — persist commits exceptions
and suspense, assemble saves the pack — and its store writes are
upserts keyed by deterministic ids, which made a repeated run
look harmless in the counts while being destructive in the
record: `save_pack` overwrites the pack in place, so a second run
reverted an approved pack to proposed and erased a rejection;
`add_exception`'s upsert reset a day's exceptions to open,
discarding the workbench's assignee and handling notes; the day
report, reading the allocations index, stopped footing against a
doubled index on the store whose save appended. (All three
behaviours were established by running a day twice against a
scratch store and diffing the books, not by reading the code.)

The guard lives in `pipeline.run_day`, on the audit trail: each
attempt writes `day_run_started` / `day_run_completed` events
whose subject is the day, and the day's standing is read off the
order of that trail — complete when the last started attempt is
followed by a completion, orphaned when a started attempt is the
last word (counts alone misread a recovered day as interrupted
forever, because a recovery adds its own started/completed pair
above the orphan's started). A decided pack refuses a second run
on the pack's own evidence, so books written before run records
existed are guarded too; the refusal is itself an audit event and
the run's only trace. The recovery path — the one legitimate
second run — stamps the orphan `day_run_interrupted`, cleanses
the dead attempt's allocation-index rows for the day's
still-proposed packs (the index is an index of pack entries, so a
combination pack the rerun leaves untouched has its rows
re-derived from the pack as it stands), and carries the day's
exceptions' handling state (status, assignee, notes) across the
re-persist: the regenerated record's code, amount and detail
stand, but the desk's work on the crashed attempt's queue is not
the recovery's to undo. Attempts run on attempt-scoped checkpoint
threads (`day-<date>-a<N>`), so no attempt ever resumes another's
half-written graph state. Two store contracts were aligned to
make the in-memory double tell the same truth as the SQL stores,
and the conformance battery now pins both: suspense `add` is
insert-only everywhere, and the allocations index is keyed by
(bank line, installment) everywhere — a second save indexes
nothing twice.

The trail guard, though, is read-then-write, and when it shipped
this document's position was that cross-process simultaneity was
the one thing it could not make true: two processes starting the
same day at the same moment could both read the same trail and
both pass. That is now made true, at the store. Before any of
the run's own writes, the starter takes the day's claim — the
`day_claims` record (migration `0006` in both dialects, and the
in-memory store's equivalent), keyed by the day itself. The
first starter's insert wins atomically (a unique key, an
insert-or-fail); a second starter, even mid-run of the first,
meets the standing claim and is refused through the same
governed path as any repeat — API 409 / CLI exit 1, the refusal
audited — naming the claiming attempt. The claim alone cannot
distinguish a live attempt from a dead one, so it is paired with
a per-day run lock held for the run's duration: a Postgres
advisory lock on a dedicated connection (session-scoped, so it
frees itself when the holder's process dies), an flock on a file
beside the SQLite database, a threading lock in memory. The
recovery path is the claim's only takeover route, and it is
gated twice: the trail must show the prior attempt orphaned (or
the claim itself must stand ahead of the trail — a starter that
died between claiming and its start being recorded), and the
recovery must win the lock, which a live attempt would still be
holding; only then does it take the claim over, by
compare-and-swap on the interrupted attempt's number and state.
A completed run's claim is the day's standing record, and a
decided pack's day is claimed permanently, because the decided
refusal precedes claim acquisition and a completed claim never
returns to running. The conformance battery pins the claim
semantics on all three stores — first claim wins, reclaim is
compare-and-swap, a completed claim is neither reclaimable nor
re-claimable, the lock has one holder — and, against Postgres,
runs a genuine two-writer race: two store instances on two
connections, released at the same moment, exactly one winner
for the claim and exactly one for the lock.

## Checkpoint retention

The checkpointer is the graph's scratch memory: a chain of state
snapshots per run, in the saver's own tables (`checkpoints` /
`writes` on SQLite; `checkpoints` / `checkpoint_blobs` /
`checkpoint_writes` on Postgres). Nothing trims it — measured on
this repository's own scratch database: 126,843 checkpoints
across 36 threads, 825 MB, from test and demo runs. The retention
policy (`checkpoints.py`) keeps the newest
`checkpoint_keep_per_thread` per thread and prunes kept
checkpoints older than `checkpoint_max_age_days`, with one
exception that outranks both: a thread's single newest checkpoint
is never pruned, so the latest state of every thread always still
loads and a run can always be inspected where it stopped.
Timestamps are read through the saver itself on SQLite (they live
inside its serialised payloads) and from the checkpoint JSON on
Postgres; the Postgres prune additionally garbage-collects a
thread's blobs whose (channel, version) no surviving checkpoint
references. The prune is an explicit operator act — a CLI
subcommand with a `--dry-run` that counts without cutting — and
the only automation is the opt-in startup hook, off by default
and logged with its counts when on: deleting anything is a
decision, and decisions here are named and on the record. The
policy's correctness is tested where it matters: after a prune,
every thread's newest state still loads through the saver with
its final report intact, and a fresh day run completes against
the pruned store.

## Combination settlements

The per-line tiers are deliberately single-line: their refusals are
what make their matches trustworthy. The cost is a real pattern —
a payer settling one bill in two wires — landing both wires in
suspense. The combination pass (`combinations.py`) is the governed
second look, placed *after* the tiers (a day-run graph node between
assemble and sweep, and a closing step of the re-match pass) so it
never competes with a per-line explanation: it only reads
wholly-unapplied suspense the tiers already failed to explain.

Three design choices carry the weight. **Per-leg pricing:** a
pair's proof grosses each leg up at the commission rate in force on
that leg's own value date — the gate's `_apply` arithmetic run in
advance — so a pair straddling a rate change is provable, and the
gate applies each leg at the rate it was proposed at, not the pack
date's rate (the one exception to the pack-date rule, recorded on
the entry). **Proposal-time suspense resolution:** a leg's item is
held resolved from the moment the pair is proposed, mirroring the
re-match recovery discipline — the day footing counts allocations
from saved pack records regardless of gate state, so holding the
item open would double-count the cash; the price is that a
proposed-but-undecided pair's cash sits outside the aging buckets,
which the day report answers by itemising the combination section
by pack status. **Rejection frees the legs:** the gate re-opens a
rejected combination's items with their original received dates,
and the leg test counts allocations only from packs that have not
been rejected, so a refused pair is proposable again — while the
gate's approval path re-verifies the pair against the target's
standing balance and refuses the whole pack, untouched, if the
books moved since proposal. Combination entries are uneditable at
the gate by construction: an editable "exact proof" is not a proof.
The search bound (pairs only, ≤12 legs per payer, same payer only,
suspected duplicates never legs) is a stated limit, not an
oversight: wider combination search is exponential, ambiguous, and
belongs to a human working the workbench.

## Non-premium lines

Classification is placed *before* the tiers, in the match node, and
is a pure config lookup (`NON_PREMIUM_RULES` in `config.py`): every
field a rule names must match, the payer comparison is whole-name
after normalisation. The conservatism is the design — the failure
mode being defended against is not an unclassified fee (it ages in
suspense, visible, recoverable) but a premium payment classified
away (invisible, unrecoverable without an audit), so the rules
accept only the carrier's own name as payer, and the bank's name
paired with a fee/interest narrative. Dispositions reuse
`PostingPack` rather than a parallel record: the gate's
self-approval refusal, second-approver rule, rejection reasons,
posting file, webhook events and audit trail all apply unchanged,
and the "ledger" is a read model over those packs, so there is one
gate implementation in the system, not two. The day footing gains
the non-premium term only additively — the note text is unchanged
on days with no dispositions, because existing readers (and the
golden set) pin it.

## The live reading set

`evals/run_live_evals.py` exists because the golden gate has a
structural blind spot: it runs the offline fallback, which reads
the synthetic text layer, so it can never fail the way a real model
fails on a messy advice. The live set grades the reader port
directly (screen → read for advices; the matcher's
candidate discipline → propose for lump sums) against gold written
by construction, and reports per-case extraction accuracy. Its
skip behaviour is part of the design: no provider configured means
SKIPPED-LIVE with exit 0 — CI stays green, and nobody can mistake
the absence of a measurement for a measurement. It is kept out of
`make evals` for the same reason: the standard gate must be
runnable by anyone who clones the repo, with no key.

## What v1 deliberately does not do

No live bank feeds (statements arrive as files and are imported;
nothing polls a bank), no general-ledger integration
(the approved pack's CSV is the hand-off), no payment execution, no
claims or reinsurance cash — premium cash application for one
carrier. Auto-application exists only as a *mark* on small exact
matches; a deployment that wants true straight-through posting for
that slice approves those packs in bulk, with the report still
naming every line. Combination settlements are pairs only (three
or more legs that together settle one item are not searched), and
proposal provenance exists only on packs assembled since the
quality report landed — older packs are ungraded, not backfilled.
