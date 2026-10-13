"""The live remittance-reading eval set: the runner's honesty and
its scoring, tested without a provider.

Two things must hold without any key in sight. First, with no
provider configured the runner reports SKIPPED-LIVE and exits 0 —
a non-result, never a fake pass, and never a gate failure (the
standard gate in run_evals.py does not include this set at all).
Second, the scoring functions grade what a reader returns: exact
extraction passes, extras and misses fail, an unfenced injection
fails before the reader is even called, and a lump-sum proposal
that reaches across brokers fails while a clean refusal passes.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from cash_recon.llm import OfflineReader, Proposal, Candidate
from cash_recon.models import RemittanceLine

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location(
    "run_live_evals", ROOT / "evals" / "run_live_evals.py"
)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class FakeReader:
    """A reader port double returning canned outputs per case."""

    name = "fake"

    def __init__(self, advice_lines=None, proposal=None):
        self._advice = advice_lines or []
        self._proposal = proposal

    def read_remittance(self, text):
        return list(self._advice)

    def propose_lump_sum(self, amount, payer, candidates):
        return self._proposal


def _advice_case():
    return next(c for c in live.load_cases() if c["case"] == "LIVE-ADVICE-MESSY-1")


def _injection_case():
    return next(c for c in live.load_cases() if c["case"] == "LIVE-ADVICE-INJECTION")


def _cross_case():
    return next(c for c in live.load_cases() if c["case"] == "LIVE-LUMP-CROSS-BROKER")


def _clean_case():
    return next(c for c in live.load_cases() if c["case"] == "LIVE-LUMP-CLEAN")


def test_no_provider_reports_skipped_live(monkeypatch, capsys):
    for var in ("LLM_BACKEND", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "ANTHROPIC_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert live.main() == 0
    out = capsys.readouterr().out
    assert "SKIPPED-LIVE" in out
    assert "not a pass" in out


def test_offline_reader_passes_every_case_as_a_smoke_signal():
    results = live.run_cases(OfflineReader(), live.load_cases())
    assert all(r["passed"] for r in results), results
    assert all(r["accuracy"] == 1.0 for r in results)


def test_scoring_counts_extras_and_misses():
    case = _advice_case()
    exact = [RemittanceLine(policy_number=g["policy"], amount=D(g["amount"]),
                            citation="x")
             for g in case["gold_lines"]]
    passed, accuracy, _ = live.score_advice_output(case, exact)
    assert passed and accuracy == 1.0
    with_extra = exact + [RemittanceLine(policy_number="NSP-9999",
                                         amount=D("1.00"), citation="x")]
    passed, accuracy, detail = live.score_advice_output(case, with_extra)
    assert not passed and "extra" in detail
    passed, accuracy, _ = live.score_advice_output(case, exact[:-1])
    assert not passed and accuracy < 1.0


def test_cross_broker_proposal_scores_as_failure():
    case = _cross_case()
    reaching = Proposal(
        allocations=[
            Candidate(policy_number="NSP-3101", installment_id="NSP-3101-I1",
                      cash_due=D("2000.00")),
            Candidate(policy_number="NSP-3103", installment_id="NSP-3103-I1",
                      cash_due=D("2400.00")),
        ],
        reasoning="sums exactly",
    )
    passed, _, detail = live.score_lump_output(case, reaching)
    assert not passed and "outside the payer's book" in detail
    passed, accuracy, _ = live.score_lump_output(case, None)
    assert passed and accuracy == 1.0


def test_clean_lump_proposal_scores_exactly():
    case = _clean_case()
    good = Proposal(
        allocations=[
            Candidate(policy_number="NSP-3104", installment_id="NSP-3104-I1",
                      cash_due=D("1800.00")),
            Candidate(policy_number="NSP-3105", installment_id="NSP-3105-I1",
                      cash_due=D("2700.00")),
        ],
        reasoning="the only exact subset",
    )
    passed, accuracy, _ = live.score_lump_output(case, good)
    assert passed and accuracy == 1.0
    passed, _, _ = live.score_lump_output(case, None)
    assert not passed


def test_injection_case_runs_the_screen_first():
    # A reader that would happily read the raw text is never given
    # it raw: run_cases screens first, and the gold lines alone
    # come back. The fake returns the gold lines for the VISIBLE
    # text; the case passes only if the screen fenced the attempt.
    case = _injection_case()
    lines = [RemittanceLine(policy_number=g["policy"], amount=D(g["amount"]),
                            citation="x")
             for g in case["gold_lines"]]
    (result,) = live.run_cases(FakeReader(advice_lines=lines), [case])
    assert result["passed"] and "fenced" in result["detail"]
