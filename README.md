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
make evals       # the golden-case gate (31 cases, gold by construction)
make serve       # API + console on http://localhost:8000
```

No uv? `python3 -m venv .venv && source .venv/bin/activate`, then
`make install-pip` (the frozen, hash-verified constraints set installs
first; the package itself second, with `--no-deps`).

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

## Exceptions

Each carries a reason code, the amount at issue, and an aging clock:
`UNIDENTIFIED`, `PARTIAL`, `OVERPAID`, `NETTING_MISMATCH`,
`REMITTANCE_ARITHMETIC`, `UNKNOWN_POLICY`, `CANCELLED_POLICY`,
`DUPLICATE_PAYMENT`, and `STALE_SUSPENSE` for unapplied cash past the
configured age. For a netting mismatch the system drafts the broker
query note — what was expected, what arrived, the delta. Drafted,
never sent.

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
31–60, 60+ days) with the oldest items named.

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
scores 31 golden cases committed at `evals/golden.jsonl`: every tier,
every planted exception, the remittance readings, the injection, the
near-duplicate, and two controls (every day foots; the opening
suspense ages into the right buckets). The runner prints a
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
  report.py      the daily reconciliation report + suspense aging
  pipeline.py    the day run as a LangGraph graph
  store.py       Postgres (production) / in-memory (test double)
  objects.py     S3-compatible document storage
  api.py         FastAPI: health, run, gate, reports, console
  static/        the console (one HTML file, no dependencies)
migrations/      numbered SQL migrations, applied at startup
evals/           the golden-case runner + golden.jsonl
tests/           pytest: tiers, netting math, gate rules, footing, guardrails
```

## Honest status

The blueprint is built and passing its automated gates: 52 tests,
31/31 eval cases, every demo day footing to the cent. Live
verification against a connected model is still pending — the reader
and proposer adapters are implemented and validation-tested with
stubbed transports, and the offline fallback is what the demo and the
evals exercise. Hands-on local testing by our team is the remaining
step (see the work-in-progress note above).

## License

Apache 2.0. See `LICENSE`. Contributions: see `CONTRIBUTING.md`.
