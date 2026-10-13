# Trida AI Blueprint — Insurance Cash Reconciliation Agent
#
# Primary workflow is uv (https://docs.astral.sh/uv/):
#   make install     uv sync --extra dev
#   make demo        one reconciliation day end to end, offline (no keys)
#   make test        pytest
#   make evals       golden-case gate over the synthetic month (offline)
#   make serve       the API + console on http://localhost:8000
#   make prod-smoke  prove the production profile against real
#                    PostgreSQL + S3 (needs DATABASE_URL + S3_* env)
#
# No uv? python3 -m venv .venv && source .venv/bin/activate, then:
#   make install-pip

.PHONY: install install-pip demo test evals evals-live serve prod-smoke

install:
	uv sync --extra dev

install-pip:
	# The frozen, hash-verified set first, then the package itself with
	# --no-deps: pip refuses an editable install under a hashed
	# constraints file, so the two steps never mix.
	pip install -r constraints.txt
	pip install --no-deps -e .
	pip install "pytest==9.1.1" "httpx==0.28.1"

demo:
	uv run cash-recon-demo

test:
	uv run pytest -q

evals:
	uv run python evals/run_evals.py

# The live remittance-reading set: grades a real model's reading of
# messy free-form advices. With no provider configured it prints
# SKIPPED-LIVE and exits 0 — a non-result, never a pass. It is not
# part of the standard gate above.
evals-live:
	uv run python evals/run_live_evals.py

serve:
	uv run uvicorn cash_recon.api:app --port 8000

# The production proof, repeatable: boots the API against the real
# stores named by DATABASE_URL + S3_* and checks readiness, seeds a
# FRESH database, runs and approves a day, compares close-pack
# digests, and round-trips a document through the object store.
# Refuses to run against the SQLite/in-memory profiles. See
# scripts/prod_smoke.py and the README's "Verifying a production
# deployment".
prod-smoke:
	uv run python scripts/prod_smoke.py
