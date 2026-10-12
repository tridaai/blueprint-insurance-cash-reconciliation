# Contributing

This repository is a Trida AI blueprint: a reference prototype that a
team can take, run, and adapt. Contributions that keep it honest and
runnable are welcome.

## Ground rules

- **Synthetic data only.** Never commit real policy, bank, or remittance
  data. The fictional carrier is Northgate Specialty Insurance; keep it
  fictional.
- **Money is Decimal.** No floats anywhere near amounts. Rounding is
  half-up, at the defined points only, and always to cents.
- **Matching is code.** Tier decisions, netting math, allocation
  validation, gate rules and report footing are deterministic code. A
  model proposes; it never decides a match, a posting, or a write-off.
- **Nothing posts itself.** Every change keeps the human gate: a named
  person approves each posting pack, and the system never moves money
  or writes to a general ledger.
- **Tests and evals are the gate.** `make test` and `make evals` must
  pass before a change lands. If you change matcher behaviour, change
  the golden cases with it and say why.

## Setup

```bash
make install      # uv sync --extra dev
make demo         # one day end to end, offline
make test
make evals
```

Without uv: create a virtualenv and run `make install-pip` (the frozen
constraints set installs first, then the package with `--no-deps`).

## Shape of a change

Small, human-readable commits. Say what the change does in the subject
line the way you would tell a colleague. Do not squash history.
