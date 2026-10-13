<p align="center">
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="96">
</p>

# Blueprint — Insurance Cash Reconciliation Agent

Every morning, a specialty carrier's cash team faces the same pile:
money landed in the bank overnight, and someone has to work out which
policies it pays. Insurance makes that harder than ordinary accounts
receivable. Brokers remit **net** of their commission, so the bank
line almost never equals the billed premium. One wire covers dozens of
policies and is explained only by a remittance advice. Insureds pay
installments late, short, double, or combined. And money nobody can
identify sits in suspense and ages, while auditors ask about it first.

This blueprint is the agent we would deploy into that desk. It ingests
bank lines, billing receivables and remittance documents; matches cash
to policies in **deterministic tiers, in code**; uses a model only
where judgment is genuinely needed — reading unstructured remittance
advices, proposing an allocation for an unexplained lump sum, drafting
the broker query note; raises exceptions with reason codes for
everything it cannot settle; and stops at a **human gate**. Nothing
posts itself: a named person approves every posting pack, and the
system's output is the approved pack (a posting file a human uploads
to the accounting system) plus the audit record and a daily
reconciliation report whose control totals must foot to the cent.

The fictional carrier is **Northgate Specialty Insurance**, selling
through eight brokers and a direct book. All policies, brokers, bank
lines and people are synthetic.

> Reference prototype. All policies, brokers, bank lines, remittance
> documents and metrics are synthetic. Not connected to any real
> carrier, bank, or accounting system.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

## Run it with a real model

The remittance reader and the lump-sum proposer are built for a real
LLM. Set one provider and they use it; every model output is validated
in code before it is worth anything (allocations must cite the
document text they came from; a proposal must sum to the wire to the
cent and may only name real open items).

```bash
cp .env.example .env   # then set ONE provider
```

- **OpenAI-compatible** — `OPENAI_API_KEY`, plus `OPENAI_BASE_URL` for
  any compatible endpoint: NVIDIA NIM hosted
  (`https://integrate.api.nvidia.com/v1`) or self-hosted
  (`http://localhost:8000/v1`), vLLM, and so on. `OPENAI_MODEL` picks
  the model.
- **Anthropic** — `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`).
- **Ollama** — `OLLAMA_BASE_URL` for a local model.

With no provider configured the pipeline runs on a labelled offline
fallback: a deterministic reader for advice documents that follow the
text-layer format, and an exact subset search for lump sums. It keeps
the demo and the evals runnable with no keys, and it is honest about
its limits — free-form remittance advices need a connected model.

## Quickstart

```bash
make install     # uv sync --extra dev
make demo        # Northgate's September, end to end, offline
make test        # pytest
make evals       # the golden-case gate (68 cases, gold by construction)
make evals-live  # the live reading set — needs a provider key (SKIPPED-LIVE without one)
make serve       # API + console on http://localhost:8000
make prod-smoke  # prove the production profile against real PostgreSQL + S3 (env required)
```

No uv? `python3 -m venv .venv && source .venv/bin/activate`, then
`make install-pip` (the frozen, hash-verified constraints set installs
first; the package itself second, with `--no-deps`).

Running locally needs **no database setup at all**: with nothing
configured, records, LangGraph checkpoints and document embeddings
persist to a single SQLite file (`./data/cashrecon.db`; `SQLITE_PATH`
moves it). Seed it, run days, restart the server — the desk's state
is still there, and the CLI's `seed` / `run` / `report` compose
across invocations on the file. The demo uses a throwaway SQLite
file so it starts clean every time.

**Switching stores is an env change only.** Set `DATABASE_URL` and
the same code runs on PostgreSQL — the SQLite dialect in
`migrations/sqlite/` mirrors the Postgres migrations in
`migrations/` table for table, money stays decimal-exact TEXT in
both, and the near-duplicate check computes its cosine in code under
SQLite instead of leaning on pgvector. Nothing else in the app knows
which store is running; `/readiness` reports the active one.

Production topology, locally:

```bash
docker compose up --build
curl localhost:8000/health
```

Compose runs the stateless API against PostgreSQL (records, LangGraph
checkpoints, pgvector document embeddings) and an S3-compatible store
(SeaweedFS's gateway, service name `minio`) holding the remittance
documents. The API applies the numbered SQL migrations in
`migrations/` on startup.

## Verifying a production deployment

`make prod-smoke` turns the production proof into one repeatable
command. Point it at real services — PostgreSQL with the pgvector
extension and any S3-compatible store; the topology above, or the
same services stood up natively:

```bash
DATABASE_URL=postgresql://cashrecon:cashrecon@localhost:5433/cashrecon_smoke \
S3_BUCKET=remittance-documents \
S3_ENDPOINT_URL=http://localhost:8333 \
S3_ACCESS_KEY_ID=minioadmin \
S3_SECRET_ACCESS_KEY=minioadmin \
make prod-smoke
```

Use a **fresh database**: the smoke seeds the synthetic month and
refuses a store that already holds bank lines rather than trampling
an existing book. It deletes nothing; what it creates stays for
inspection. The steps, each failing loudly with its name:

- **profile** — the env names the production profile. Without
  `DATABASE_URL` + `S3_*` the script refuses outright: it will not
  pass against the SQLite local profile or an in-memory object store.
- **api-readiness** — the real API boots against those stores and
  `/readiness` must report ready, the postgres profile, and the S3
  object store.
- **seed** — the dataset lands through the production stores:
  records and embeddings in Postgres, documents in the bucket.
- **run-day** — one September day runs through the real pipeline
  (LangGraph checkpointer included) and its report must foot.
- **approve** — the day's pack is approved at the gate by a named
  controller and stands approved.
- **close-pack-digest** — the September close pack is assembled
  twice; the SHA-256 digests must be identical.
- **document-round-trip** — a remittance document reads back from
  the object store byte-identical to what the pipeline ingested.

Two deployment notes the verification surfaced:

- **The S3 endpoint must know the app's identity.** The client
  signs every request with `S3_ACCESS_KEY_ID` /
  `S3_SECRET_ACCESS_KEY`, so those credentials must be an identity
  configured on the endpoint (an IAM key pair on real S3; an
  identity in SeaweedFS's S3 configuration). A SeaweedFS with no
  identity configured rejects the signed requests.
- **Localhost endpoints must bypass any HTTP proxy.** When the app
  runs with `http_proxy` set, service endpoints on localhost (the
  S3 gateway, a self-hosted NIM, a local model server) must be in
  `NO_PROXY`, or requests route to the proxy and never arrive. The
  smoke script extends `NO_PROXY` for its own checks and says so
  in its profile step.

## How matching works

Every bank line is worked through the tiers once, in order:

| Tier | Rule |
| --- | --- |
| **T1 exact** | Policy reference present; amount equals the amount due. |
| **T2 netting** | Amount equals billed premium minus the broker's contracted commission, to the cent. |
| **T3 reference** | Reference present, amount differs: allocate what is there, never force a match; the delta becomes a PARTIAL or OVERPAID exception. Exact runs of consecutive installments land here, as do combined payments. |
| **T4 remittance** | The line is explained by a remittance advice; its lines are allocated per policy and validated (they must sum to the wire; nothing allocates to a cancelled or fully-paid policy without flagging). |
| **T5 unidentified** | No reference, no advice, no defensible proposal: to suspense. |

A broker payment that settles nothing exactly is a **NETTING_MISMATCH**
when the implied commission rate is a clean rate that is not the
contracted one — that is what deducting the wrong rate looks like —
and a partial payment otherwise. Duplicates (same payer, amount, near
dates, against already-applied cash) are checked in code before any
tier runs.

Every line ends fully accounted for, and the matcher asserts it:
**allocations + payable offsets + suspense == the bank amount, to
the cent.** (The offset term is cash netted against a return-premium
payable — see *Return premiums* below; on an ordinary line it is
zero.) One rule stands in front of the tiers: cash for a cancelled
policy meets any open return-premium payable on that policy first.
And one boundary the tiers never cross: a lump sum is settled only
within the paying broker's own book — a proposal, from any backend,
that names another broker's open item is refused whole and the wire
goes to suspense.

Broker commission is **temporal**: a broker's rate is a history of
dated periods, and the matcher prices every line at the rate in
force on the line's own value date — at match time, at the gate,
and when a return replays the arithmetic. A rate change mid-book
therefore reprices nothing retroactively: earlier cash keeps the
rate it was matched under, later cash nets at the new rate, and a
wire netted at a superseded rate is judged against the rate in
force, not the one the broker used.

## Multi-currency cash application

Northgate bills in more than one currency, and the second currency
(November's book: a broker whose policies are billed in EUR, with
one USD policy alongside) is handled the way a cash desk actually
handles it — in currency first, converted only on paper:

- **Matching is in currency.** Every record names its denomination
  (ISO code): bank lines, receivables and installments, suspense
  items, exceptions, returns. A line is worked only against open
  items denominated in the line's own currency — exact, netting
  (the broker's commission applies in that currency), remittance
  lines, and lump-sum candidates alike. A combination pair must
  share one currency with its target, and the duplicate fingerprint
  includes currency: the same number in another currency is
  different money, not a duplicate.
- **Cross-currency settlement is refused by design.** Cash is never
  converted to hunt a match. A wire citing a policy billed in
  another currency — even for exactly the amount that would settle
  it in that currency — raises the new **`CURRENCY_NO_OPEN_ITEMS`**
  exception and sits in suspense in its own denomination, as does
  an unexplained wire whose broker holds open items only in other
  currencies. A lump-sum proposal naming another currency's item is
  refused whole.
- **Conversion is a recording act, in code, at governed rates.**
  The FX table (`config.py`, in the broker-rate-history mould)
  holds rates by effective period — EUR 1.08, changing to 1.10
  mid-November — quoted as functional (USD) units per unit of
  currency. When an allocation is created for a foreign-currency
  line, the conversion is computed at the rate in force on the
  line's value date, rounded half-up at that one point, and **all
  three figures are recorded on the allocation**: the cash amount,
  the rate used, and the functional amount. The per-line audit
  export and the posting file both show all three.
- **Recorded conversions are never re-priced.** A gate edit that
  moves the cash re-derives the functional amount at the recorded
  rate. A returned foreign-currency line is reversed in its own
  currency by the ordinary replay — which works in billed terms —
  so its recorded conversion stands as recorded, even when the
  return is recorded after the rate has moved. A re-send is new
  cash and converts at its own date's rate.
- **Suspense and reports keep denominations apart.** Suspense
  items hold their currency and the aging reports totals per
  currency — EUR is never added to USD. The day report foots **per
  currency** and adds a functional (USD) translation of the day
  with the rates used named. The close pack's suspense identity
  holds per currency (its headline roll is the functional
  currency's), and the pack also shows functional equivalents of
  closing suspense at month-end rates, labelled as translations,
  not postings. The broker scorecard and the trend watch keep
  per-currency figures separate and compute no blended rates.

November's planted proofs, gold by construction: an EUR exact
match; an EUR installment plan netted on either side of the FX
change, each line converting at its own date's rate; the first of
those lines returned after the move and re-sent (the reversal at
the recorded rate, the re-send at the new one); an EUR wire citing
the broker's USD policy and a USD wire citing the EUR plan for
exactly its net due — both `CURRENCY_NO_OPEN_ITEMS`, settling
nothing — and the USD policy then settled properly in USD.
November's close foots per currency: USD opening 85,077.74
(October's closing, the continuity control) + 9,000.00 received =
94,077.74 closing; EUR opening 0.00 + 4,500.00 received = 4,500.00
closing, with the EUR balance translating to 4,950.00 at the
month-end rate of 1.10.

## Combination settlements

The tiers work one bank line at a time, and a payer who settles a
bill in two wires defeats them honestly: neither wire equals
anything due, and each lands in suspense as UNIDENTIFIED. A
**combination pass** (`combinations.py`) runs after the tiers — at
the end of every day run, and again at the end of the suspense
re-match — and looks for pairs the code can prove:

- both legs are wholly unapplied cash of the **same payer** (an
  open suspense item holding a whole bank line; a line carrying a
  suspected DUPLICATE_PAYMENT is never a leg);
- the target is one open installment on that payer's own book;
- each leg is priced at the commission rate in force on **its own
  value date** — the gate's gross-up arithmetic, so a pair can
  straddle a rate change — and the two priced values sum to the
  installment's open billed balance **exactly, to the cent**.

A proven pair becomes a proposal in a `PACK-COMB-<date>` pack at
the same human gate, both legs itemised (line, date, amount, the
rate each was priced at). Approval applies both legs atomically:
the gate re-verifies the pair against the installment's standing
balance before writing anything, and refuses the pack whole if the
books have moved. Rejection frees the legs — their suspense items
re-open and the pair can be proposed again. Combination entries
cannot be edited at the gate: a pair settles exactly as proposed,
together, or not at all. The search is bounded and says so: pairs
only (never triples), same-payer wholly-unapplied legs only, at
most 12 legs considered per payer. October plants the proofs: two
bills settled by wire pairs weeks apart (one straddling Fernway's
rate change), a pair one cent short that must not be proposed, and
an exact sum across two different payers that must not be proposed
either. The day report and the close pack account for combination
settlements as their own section; the footing identity is
unchanged, to the cent.

## Non-premium lines

Not every credit on the statement is premium. The bank rebates a
fee, the trust account earns interest, the carrier moves its own
money between its own accounts. Sent through the tiers, those
lines land in UNIDENTIFIED suspense and pollute the aging auditors
watch — so before the tiers, a **code-first classifier**
(`nonpremium.py`, rules in `config.py`) marks a line
**NON_PREMIUM** with a sub-kind — `BANK_FEE`, `INTEREST`,
`OWN_TRANSFER`, `OTHER_NON_PREMIUM` — and routes it out of the
premium pipeline into a disposition pack (`NPACK-<date>`) at the
same gate: named approver, self-approval refused, decided like
everything else. Approval posts the line to a **non-premium ledger
view** (`GET /api/non-premium`, `cash-recon non-premium`); it
never touches a receivable and never enters suspense aging.

The classifier is deliberately conservative: a rule fires only
when **every** field it names matches exactly — the payer
comparison is against the whole normalised payer name, so an
insured called "Northgate Builders LLC" paying real premium is
not the carrier, and a bank line whose narrative names no rule's
text is not classified either. Anything ambiguous stays
UNIDENTIFIED suspense exactly as before, and the model never
classifies these lines: classification is a config lookup, not a
judgment. The day report carries a non-premium section and the
footing identity gains the term (bank == applied + netted +
suspense + non-premium dispositions); the close pack carries the
month's non-premium totals by sub-kind. October plants an interest
credit, an own-account transfer, a fee rebate — and the lookalike
premium payment that must not be classified away.

## Installments, worked one by one

Each installment is its own open item, with its own due date and
sequence number, and the allocation order is code, stated on the
record: cash identified for a policy applies to the **oldest open
installment first**. The one exception is a payer whose structured
reference names a specific installment (`NSP-1003-I3`): that
installment is settled first, and any remainder flows to the oldest
open installment after it, with the flow written on the allocations.
A free-text note indicating a different installment ("for installment
2") is not an instruction — the cash still applies oldest-first, and
the outcome carries an **INTENT_DIFFERS** flag so the desk sees the
payer's apparent intent differed. Money is never left sitting in
suspense over a guess about intent. Overpaying the policy's whole
remaining balance stays an OVERPAID exception, as before.

## Bringing in bank statements

Lines do not have to arrive in the dataset's own shape. A statement
importer (`POST /api/statements/import`, `cash-recon
import-statement`, or the console's Statement import tab) takes the
generic CSV a bank exports — date, description, reference, and
either a debit/credit pair or one signed amount, with the column
mapping a small alias config in code — and converts it into the same
bank-line records the tiers consume. Imported lines are
indistinguishable downstream. The import is all-or-nothing: every
row parses before anything is stored, and when the file carries
trailer controls (a `CONTROL TOTAL` / `CONTROL COUNT` row) the
parsed credits and row count must equal them exactly or the whole
import is refused with the discrepancy stated. Re-importing the same
statement cannot double-apply cash: each line's id is a digest of
its dedupe key — bank reference + amount + value date — so
already-seen lines are reported as skipped. Only credits are
imported; debit rows are counted and left behind. A worked example
ships at `data/example-statement.csv`.

## Exceptions

Each carries a reason code, the amount at issue, and an aging clock:
`UNIDENTIFIED`, `PARTIAL`, `OVERPAID`, `NETTING_MISMATCH`,
`REMITTANCE_ARITHMETIC`, `UNKNOWN_POLICY`, `CANCELLED_POLICY`,
`DUPLICATE_PAYMENT`, `CURRENCY_NO_OPEN_ITEMS` (the payer has
nothing open in the currency the cash arrived in — see
*Multi-currency cash application*), and `STALE_SUSPENSE` for unapplied cash past the
configured age. For a netting mismatch the system drafts the broker
query note — what was expected, what arrived, the delta. Drafted,
never sent.

**Re-match.** Information lands after cash does: a broker's corrected
advice, a policy booked after its wire arrived. The re-match pass
(API, the console's Suspense tab, or `cash-recon rematch`) takes
open suspense whose line has no allocations yet and works it through
the same tiers against the books as they stand now — documents that
arrived late are screened and read first, exactly as a day run would
handle them, and a corrected advice supersedes the one that failed.
Recoveries are proposals, not postings: they land in a
`PACK-REMATCH-<date>` pack at the same human gate. An item that
still does not settle stays in suspense with its reason updated to
the now-correct code, and every attempt — tiers run, outcome — is
on the item's audit trail.

**The workbench.** The queue has an owner and a memory (console
Workbench tab, `GET /api/workbench`, `cash-recon workbench`).
Every open exception can be assigned to a named member of the cash
team — the roster is data, and an off-roster name is refused — and
can carry dated handling notes: append-only, attributed, with no
edit or delete path anywhere. Assignment and notes are handling
metadata only; they never change an amount, a code, or a
disposition. Dispositions still happen only through the governed
paths above and below. The view runs oldest first, filters by
code, broker, assignee, or unassigned, and its counts by code foot
to the queue totals.

## The gate

A posting pack is assembled per day by a named person and approved by
a **different** named person — self-approval is refused in code.
Delta write-offs above the configured single-approver limit
(`WRITE_OFF_SINGLE_LIMIT`, default 250.00) need a second approver,
different again. Exact T1 matches at or below
`AUTO_APPLY_THRESHOLD` (default 500.00) are marked auto-appliable —
still listed on the pack and the day's report. Approval is the only
moment receivables change, and every applied cent keeps its trace:
bank line, allocation, pack, approver. This system never moves money
and never writes to a general ledger.

**Editing a proposal at the gate.** A proposed pack's entries can
be corrected before approval — by a named approver-side person,
never the assembler — one governed act that adjusts amounts and/or
targets atomically. Each change is recorded on the entry beside the
original it replaced, and the edit is refused whole if any bank
line's entries would stop summing to the total they summed to
before: an edit redistributes a line's cash, it does not create or
destroy any. Decided packs cannot be edited; they are history.

## Returned payments

Cash that was applied can come back: an ACH debit bounces, a wire
is recalled, a cheque is dishonoured. Recording the return (the
console's Day report tab, `POST /api/bank-lines/{id}/returns`,
`cash-recon return`) un-applies the line's allocations **exactly**
— the reversal deltas are derived by replaying the approved packs
through the gate's own apply arithmetic, so every settled item
re-opens at its prior balance and prior status, broker gross-ups
included, to the cent. Nothing approved is rewritten: the packs
that carried the line keep their entries byte for byte and gain a
`RETURN_AFFECTED` flag, and the return stands as a reversing entry
on the day report of the day it is recorded and in the month-end
close pack, whose suspense roll names returned cash as its own
term. Suspense the line held is released by the return — the money
went back to the payer. A returned line cannot be re-applied
as-is; if the payer re-sends, the money arrives as a **new** bank
line, is matched on its own merits (the duplicate check treats a
returned line's fingerprint as reusable), and is linked back to
the returned original on its record. The whole chain — application,
return, re-application — reads off the line's history
(`GET /api/bank-lines/{id}/history`, `cash-recon history`).
Full-line returns only: a bank returning part of a line is refused
at recording time, with the reason stated.

## Return premiums

Everything above is money arriving. A policy cancelled mid-term
reverses the flow: the carrier owes the unearned premium back.
Recording a cancellation (`POST /api/policies/{n}/cancel`,
`cash-recon cancel`, the console's Packs tab) computes the return
**in code, pro-rata by days** — billed premium × term days elapsed
÷ term days, half-up at the cent — against what the installments
say was actually paid, and the computation is printed line by line
on the cancellation record. The difference opens a
**return-premium payable**: owed to the insured, or to the broker
of record where the broker collected the premium — stated on the
payable, because the money goes back the way it came in. (When the
insured is behind — earned ahead of paid — the record shows what
they owe and no payable opens.)

Cash that later arrives for the cancelled policy **nets against
the payable first**, in code, before the cancelled-policy
exception: the offset rides the day's posting pack like any other
entry and reduces the payable only when the pack is approved, with
the chain — opened, netted, paid — readable on the payable itself.
Any remainder follows the existing paths (exception, suspense).

Settling a payable takes a **payout pack** at the same human gate
as posting: assembled by a named person from open payables, never
self-approved, and a pack above the single-approver limit (the
same `WRITE_OFF_SINGLE_LIMIT` the gate uses for write-offs) needs
the second approver. Approval re-checks every payable against the
pack — if cash netted against one meanwhile, the pack is refused
whole rather than paying a stale figure. This system pays nothing:
the approved output is a payout file (CSV) a human hands to the
bank, with the approvals on the audit trail.

The controls keep the two directions of cash apart on purpose. The
day report gains a **netted** term — bank equals applied plus
netted plus suspense — and return-premium control lines (payables
opened, netted, paid out under approved packs). The close pack
rolls the **payable ledger on its own identity** — opening
payables + opened − netted − paid = closing payables — because a
payable is money owed, not cash held, and forcing it into the bank
identity would make both numbers lie.

## The daily report

The control the day is judged by: bank total vs allocated vs posted
vs in-suspense, by day and by broker. Bank must equal applied plus
suspense to the cent — plus, on a day when cancelled-policy cash
netted against a payable, the netted amount as its own named term —
or the report says so in words and refuses to call the day
reconciled. The day's return premiums stand on the report as their
own control lines: payables opened, cash netted, payouts approved. Suspense is aged in buckets (0–7, 8–30,
31–60, 60+ days) with the oldest items named. Cash recovered by the
re-match pass is shown as its own line for the day the recovery ran,
by bank line — never folded silently into the day's allocations.
Returns recorded on the day stand on the report as their own
control line — count and total — and the day a returned line was
received restates its allocations net of the return, beside the
gross figures, neither netted away silently. On a day that takes
more than one currency, the report foots **per currency** — each
currency's bank total against its own applied, netted, suspense,
and non-premium — and carries a functional (USD) translation of
the day with the governed rates used named; the translation is a
reporting conversion, never the control.

## The day-open checklist

The console opens on a **Today** tab (`GET /api/today?date=…`,
`cash-recon today`): the desk's morning in the order it is worked —
statement imported? day run complete? exceptions open (how many,
what is at issue, which is oldest)? packs awaiting the gate?
suspense re-match tried against the books as they now stand?
cancellations and returns recorded? Each step reads the same read
models the tabs use and links to the tab that does the work; the
checklist holds no state of its own, so a step is done exactly when
the act underneath it has happened.

## Proposal quality

What the gate did with the model's work, measured from the gate's
own records (`GET /api/proposals/quality`, `cash-recon quality`,
the console's Packs tab, and a section of the close pack). Every
allocation that stands on a model proposal — a remittance reading
or a lump-sum proposal, stamped with its kind and backend when the
pack is assembled — is graded by the approver's actual act:
**accepted as proposed**, **accepted with edits** (with the total
value edited), or **rejected** with its pack. Code-matched tiers
are not graded; the model made no claim there. The report groups
by proposal kind and foots to the decided packs, and it scores the
model's work — no individual human is scored anywhere in it.

## The month-end close pack

The month gets the control the day gets, at period scale (console
Month close tab, `GET /api/close/{YYYY-MM}` + `/text`,
`cash-recon close`). The control roll runs over the suspense
ledger: opening suspense (aged, by bucket) + cash received into
suspense in the month − applied from suspense = closing suspense,
with every item's position reconstructed from the records so the
closing figure cross-checks against the ledger itself. If the two
disagree, or an item cannot be traced to a bank line, the pack says
**NOT FOOTED** in words and names the difference. The pack assembles
every posting pack approved for the month (with its approvers),
every exception opened in the month and every one closed by month
end with its disposition, suspense aging at month end, and the
broker scorecard scoped to the month. The month's payment returns
stand in the pack as reversing entries — count and total as a
control line, each return listed, the packs it reversed named and
flagged — and cash a return pulled out of suspense is its own term
in the roll, so the month foots with the returns inside it, not
around it. The pack also carries the month's proposal quality:
what the gate accepted as proposed, accepted with edits, and
rejected. Approved write-offs stand in
the roll as their own control line: they dispose of receivable
deltas, not of suspense cash, so they are reported, never netted
into the suspense identity. The pack also rolls the **payable
ledger** on its own identity — opening payables + payables opened
in the month − cash netted against them − payouts under approved
payout packs = closing payables — each position reconstructed from
the payables' own chains at the month boundaries, with its own
footed flag; it never enters the suspense roll above it, because
payables are money owed back, not cash held. Export is one JSON plus a human-readable
text rendering of the same body, with a SHA-256 digest over it —
the body carries no wall-clock stamps, so a pack regenerated from
unchanged books is byte-identical. Assembling the pack changes
nothing: closing a month here is a report, not a lock. On a book
holding more than one currency the suspense roll runs **per
currency** — the identity holds in each denomination, the headline
roll is the functional currency's — and the pack carries
functional equivalents of the closing suspense at month-end
governed rates, labelled as translations, not postings.

## The broker scorecard

A read model over everything on the books, one row per broker
(console tab, `/api/brokers/scorecard`, `cash-recon scorecard`):
remittances received, the money in and where it stands, the
**clean-netting rate** — of the lines that settled at a netting
tier, the share that raised no exception — netting mismatches and
their total delta, the average days their cash sat unapplied, and
the exceptions still open against them, by code. The money columns
use the daily reports' own attribution, so they foot to the sum of
the days' by-broker splits. The query notes drafted for a broker's
mismatches hang off their row — view only. It scores brokers'
paperwork, never people, and it sends nothing.

## The per-line audit export

For any bank line, one artifact traces every cent and every act
(console Day report tab, `GET /api/bank-lines/{id}/audit-export`
and `/text`, `cash-recon audit-export`): the line as received —
its source (dataset, statement import, or manual) with the import
batch id where it has one; the tiers attempted and their outcomes
in order, recorded by the matcher as it climbed the ladder, not
reconstructed afterwards; every allocation with its basis (gross
or net of commission, the installment it settled, its tier or its
proposal kind and proposer) and every governed edit with
before/after figures; every exception raised against the line
with its handling chain; re-match attempts; the gate history of
every pack that carried it — assembler, edits, approvals,
rejections with reasons; a return and its re-sends linked both
ways; and the line's current final state. A footing block inside
the export accounts for the line — allocations posted and at the
gate, payable offsets, open suspense, reversals, the returned
amount — and either says the line foots to the cent or says so,
with the difference. Like the close pack, the body is canonical
and wall-clock-free under a SHA-256 digest, so regeneration from
the same books is byte-identical; and building it changes
nothing — a test pins the books identical before and after. Lines
worked before attempt recording landed say so in their export
rather than wearing an invented ladder.

## The match-quality trend watch

Across the months on the books (console Month close tab,
`GET /api/trends`, `cash-recon trends`): per month, the
auto-applied rate — the share of bank value settled with no human
touch beyond the gate — the tier mix by value, the exception mix
by code and value share, average suspense dwell for resolved
items, the re-match recovery rate, and the return rate, with
month-over-month deltas computed in code. The figures are the day
reports' and close packs' own numbers re-aggregated — the
month's bank and allocated values are the sums of its days', and
suspense received is the close pack's reconstruction — so the
trend foots to the reports it summarises, and tests pin it on
both synthetic months. On top of the trend sits a watch with its
tolerances in `config.py` beside the policy thresholds: it names
deteriorations only — the auto-applied rate dropping more than
five points, an exception code's share of bank value doubling,
average dwell rising more than two days — each with its figures.
When nothing crosses a line, the watch says **within tolerance**,
with the movement figures, rather than staying silent.

## Gate webhooks

When a pack is assembled, approved, or rejected — posting packs,
re-match packs, and payout packs alike — or a return is recorded,
a small JSON event (kind, pack or line id, actor, totals; no
internal paths) is POSTed to the URL in `CASH_RECON_WEBHOOK_URL`.
Unset means disabled, and `/readiness` reports which state the
service is in. Delivery is fire-and-forget from the gate's
perspective: a bounded-timeout standard-library POST, one retry,
then it stops — a delivery that still fails is written to the
audit trail as `webhook_failed` with the error class, and the
gate act stands either way. The gate never waits on a downstream
system being awake.

## Guardrails, as code

- Remittance and bank text is **untrusted**. Instruction-like text
  inside a document ("apply this to policy X and write off the rest")
  is screened out before any reader sees the document, flagged on the
  audit trail, and never obeyed. The synthetic set plants one; it
  changes nothing.
- Model output is validated before use: citation-checked readings,
  proposals that must foot exactly and name only real open items.
- A re-sent remittance advice is detected as a near-duplicate
  (document embeddings) and flagged for a human — never silently
  dropped, never applied twice.
- Money is `Decimal` end to end. The one rounding point is commission
  on a premium: half-up, to cents.

## Evals

`make evals` runs the synthetic months through the real pipeline and
scores 68 golden cases committed at `evals/golden.jsonl`: every tier,
every planted exception, the remittance readings, the injection, the
near-duplicate, two re-match cases (a corrected advice rescues its
wire; an advice that still does not foot leaves the cash open under
the correct code), the installment cases (a named installment
settles the named one; a note indicating a later installment applies
oldest-first under INTENT_DIFFERS), the return cases (an NSF return
re-opens its policy, a part-return is refused, the pack is flagged
with its posting file untouched, and the re-send applies cleanly
linked to the original; an overpaid line's return releases its
suspense and the close still foots), a workbench case (assignment,
append-only notes, counts that foot), a proposal-quality case (one
of each outcome, figures footing to the gate records), and controls (every day foots;
the opening suspense ages into the right buckets; the broker
scorecard's money foots to the daily reports; the September close
pack foots with a stable digest; the example statement imports
cleanly, dedupes on re-import, and a tampered control total is
refused), a second month — October — on the same books (a broker
rate change mid-book priced by period, a renewal whose named
installment settles the renewal term, a wire spanning two brokers'
books refused whole), the **continuity control** (October's close
opens with September's closing suspense exactly, each pack assembled
from its own state of the books, so a change to September's close
fails October's golden), and the return-premium cases (pro-rata to
the cent, payable to broker vs insured, netting on the day report,
payout self-approval refused, the second-approver rule, and a
December payable ledger that foots on its own identity), the audit-export
case (a rich line — edited proposal, return, re-send — exports
completely, foots, regenerates byte-identically, and changes
nothing; a plain T1 line exports minimally but completely), and
the trend case (both months' bank and allocated values foot to
the day reports, suspense received agrees with both close packs,
and the watch compares the two months by name), the combination
cases (October's two wire-pair settlements proposed and applied to
the cent — one straddling the rate change — with the cent-short
near miss and the cross-payer exact sum both left open and
unproposed), and the non-premium cases (the interest credit, the
own-account transfer and the fee rebate each classified to the
right sub-kind, standing in approved disposition packs, with no
suspense item and no allocation; the lookalike premium payment
settling as ordinary T1 premium), and the multi-currency case
(November in its own store, so the trend watch keeps comparing
September and October: the EUR exact and netting matches with
their recorded conversions at each date's rate, the return after
the FX move reversing at the recorded rate and the re-send
converting at the new one, both cross-currency wires settling
nothing under CURRENCY_NO_OPEN_ITEMS, the per-currency day
sections and close rolls, and the month-end translations). The runner prints a
`GATE: PASS` / `GATE: FAIL` verdict and exits non-zero on failure.
Read the set for what it is: cases the builders wrote, passing by
construction — a regression gate, not an accuracy claim about your
book. When matching gets a case wrong in a new way, the case goes
into the set first, then the behaviour changes.

### The live reading set

The golden gate cannot measure the one job the model actually does
here — reading a broker's free-form remittance advice — because
the offline fallback reads the synthetic text layer the dataset is
written in. `evals/live_cases.jsonl` is the instrument for that
job: deliberately messy advices (a rambling email with comma
amounts; a line whose policy reference is missing — only the
numbered line is gold; a ragged table paying one policy twice; an
injection attempt that must be fenced by the document screen
before any reader sees it) and two lump sums (one explainable only
by reaching across two brokers' books, which must be refused; one
with exactly one exact same-broker explanation). Run it with a
provider configured — the same environment variables as the
service:

```bash
make evals-live    # or: uv run python evals/run_live_evals.py
```

The runner prints extraction accuracy per case and exits non-zero
on any failure. With **no** provider configured it prints
`SKIPPED-LIVE` and exits 0: a deliberate non-result, never a fake
pass, and the standard gate above neither includes nor depends on
this set. Until a key is available, the honest state is that the
model's reading is unmeasured — the adapters are tested with
stubbed transports, and this set is what a first live run grades.

## Layout

```
src/cash_recon/
  models.py      the records: brokers, receivables, bank lines, packs
  money.py       Decimal money, the one rounding point
  config.py      policy thresholds (auto-apply, write-off limit, stale age) + the trend watch's tolerances + the non-premium rules + the governed FX rate table
  fx.py          foreign exchange: rates priced by date, conversion in code (functional amounts recorded, never re-priced)
  dataset.py     the synthetic Northgate months (September + October, and November's second-currency book), gold by construction
  matcher.py     the five tiers, in code, with the footing assertion
  combinations.py  the combination pass: same-payer wire pairs that settle one bill exactly
  nonpremium.py  the non-premium classifier + disposition packs + the ledger view
  llm.py         the model port: reader, proposer, note drafter
  documents.py   the document screen + near-duplicate detection
  gate.py        packs, approvals, entry edits, write-offs, the posting file
  returns.py     returned payments: exact reversal, pack flags, the line's history
  cancellations.py  mid-term cancellations: pro-rata return premiums, the payable they open
  payouts.py     payout packs: payables settled at the gate, the payout file
  today.py       the day-open checklist over the same read models
  workbench.py   the exception workbench: assignment, handling notes, the queue view
  report.py      the daily reconciliation report, suspense aging, the broker scorecard, proposal quality
  close_pack.py  the month-end close pack: control roll, digest, JSON + text export
  line_export.py the per-line audit export: attempts, allocations, gate history, footing, digest
  trends.py      the match-quality trend watch: per-month figures, deltas, named deteriorations
  webhooks.py    gate webhooks: fire-and-forget events, one retry, failures audited
  statement.py   the bank statement importer: generic CSV in, bank lines out
  pipeline.py    the day run as a LangGraph graph
  rematch.py     the suspense re-match pass: late information, same tiers, same gate
  store.py       Postgres (production) / SQLite (local) / in-memory (tests)
  objects.py     S3-compatible document storage
  api.py         FastAPI: health, run, gate, reports, console
  static/        the console (one HTML file, no dependencies)
migrations/      numbered SQL migrations (PostgreSQL) + sqlite/ (SQLite dialect)
evals/           the golden-case runner + golden.jsonl; the live reading set (live_cases.jsonl + run_live_evals.py)
tests/           pytest: tiers, netting math, gate rules, footing, guardrails
```

## Honest status

The blueprint is built and passing its automated gates: 179 tests,
67/67 eval cases, every demo day footing to the cent. Live
verification against a connected model is still pending — the reader
and proposer adapters are implemented and validation-tested with
stubbed transports, the offline fallback is what the demo and the
golden evals exercise, and the live reading set (above) stands ready
to grade a real model the day a key is configured; until then it
reports SKIPPED-LIVE, honestly unmeasured. Hands-on local testing by our team is the remaining
step (see the work-in-progress note above).

## License

Apache 2.0. See `LICENSE`. Contributions: see `CONTRIBUTING.md`.
