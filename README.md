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
make evals       # the golden-case gate (38 cases, gold by construction)
make serve       # API + console on http://localhost:8000
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
**allocations + suspense == the bank amount, to the cent.**

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
`DUPLICATE_PAYMENT`, and `STALE_SUSPENSE` for unapplied cash past the
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

## The daily report

The control the day is judged by: bank total vs allocated vs posted
vs in-suspense, by day and by broker. Bank must equal applied plus
suspense to the cent, or the report says so in words and refuses to
call the day reconciled. Suspense is aged in buckets (0–7, 8–30,
31–60, 60+ days) with the oldest items named. Cash recovered by the
re-match pass is shown as its own line for the day the recovery ran,
by bank line — never folded silently into the day's allocations.

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
broker scorecard scoped to the month. Approved write-offs stand in
the roll as their own control line: they dispose of receivable
deltas, not of suspense cash, so they are reported, never netted
into the suspense identity. Export is one JSON plus a human-readable
text rendering of the same body, with a SHA-256 digest over it —
the body carries no wall-clock stamps, so a pack regenerated from
unchanged books is byte-identical. Assembling the pack changes
nothing: closing a month here is a report, not a lock.

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

`make evals` runs the synthetic month through the real pipeline and
scores 38 golden cases committed at `evals/golden.jsonl`: every tier,
every planted exception, the remittance readings, the injection, the
near-duplicate, two re-match cases (a corrected advice rescues its
wire; an advice that still does not foot leaves the cash open under
the correct code), the installment cases (a named installment
settles the named one; a note indicating a later installment applies
oldest-first under INTENT_DIFFERS), and controls (every day foots;
the opening suspense ages into the right buckets; the broker
scorecard's money foots to the daily reports; the September close
pack foots with a stable digest; the example statement imports
cleanly, dedupes on re-import, and a tampered control total is
refused). The runner prints a
`GATE: PASS` / `GATE: FAIL` verdict and exits non-zero on failure.
Read the set for what it is: cases the builders wrote, passing by
construction — a regression gate, not an accuracy claim about your
book. When matching gets a case wrong in a new way, the case goes
into the set first, then the behaviour changes.

## Layout

```
src/cash_recon/
  models.py      the records: brokers, receivables, bank lines, packs
  money.py       Decimal money, the one rounding point
  config.py      policy thresholds (auto-apply, write-off limit, stale age)
  dataset.py     the synthetic Northgate month, gold by construction
  matcher.py     the five tiers, in code, with the footing assertion
  llm.py         the model port: reader, proposer, note drafter
  documents.py   the document screen + near-duplicate detection
  gate.py        packs, approvals, write-offs, the posting file
  report.py      the daily reconciliation report, suspense aging, the broker scorecard
  close_pack.py  the month-end close pack: control roll, digest, JSON + text export
  statement.py   the bank statement importer: generic CSV in, bank lines out
  pipeline.py    the day run as a LangGraph graph
  rematch.py     the suspense re-match pass: late information, same tiers, same gate
  store.py       Postgres (production) / SQLite (local) / in-memory (tests)
  objects.py     S3-compatible document storage
  api.py         FastAPI: health, run, gate, reports, console
  static/        the console (one HTML file, no dependencies)
migrations/      numbered SQL migrations (PostgreSQL) + sqlite/ (SQLite dialect)
evals/           the golden-case runner + golden.jsonl
tests/           pytest: tiers, netting math, gate rules, footing, guardrails
```

## Honest status

The blueprint is built and passing its automated gates: 92 tests,
38/38 eval cases, every demo day footing to the cent. Live
verification against a connected model is still pending — the reader
and proposer adapters are implemented and validation-tested with
stubbed transports, and the offline fallback is what the demo and the
evals exercise. Hands-on local testing by our team is the remaining
step (see the work-in-progress note above).

## License

Apache 2.0. See `LICENSE`. Contributions: see `CONTRIBUTING.md`.
