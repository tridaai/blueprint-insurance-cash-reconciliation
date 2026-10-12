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

**Netting mismatch vs partial.** Both are "less than expected". The
distinguishing test is the implied commission rate: if
`(gross − received) / gross` is a clean rate (to a tenth of a percent)
that is not the contracted rate, the broker deducted *a* rate — the
wrong one — and the line is a NETTING_MISMATCH with the cash held
unapplied. Otherwise it is a part-payment: allocated, with the
shortfall a PARTIAL exception and the balance still collectable.

**Every cent accounted, asserted.** A matcher outcome must satisfy
`allocations + suspense == bank amount`; the matcher asserts it per
line, and the daily report re-derives the identity over the whole
day. A day that does not foot is reported NOT reconciled, in words.

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
second, different approver, and both names land on the pack.

## Stores

- **PostgreSQL** (production): one table per record type, payloads as
  JSONB with amounts as decimal strings, key columns beside them for
  the queries the desk runs. Numbered SQL migrations in `migrations/`
  are applied by the store at startup; `0002` adds the pgvector
  embedding table. LangGraph checkpoints use the Postgres saver in
  the same database when `DATABASE_URL` is set.
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

No bank feeds (lines arrive as data), no general-ledger integration
(the approved pack's CSV is the hand-off), no payment execution, no
claims or reinsurance cash — premium cash application for one
carrier. Auto-application exists only as a *mark* on small exact
matches; a deployment that wants true straight-through posting for
that slice approves those packs in bulk, with the report still
naming every line.
