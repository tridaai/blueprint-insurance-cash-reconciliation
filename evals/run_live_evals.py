"""Live-mode remittance-reading evals: grading a real model at the
one job the model actually does in this blueprint — reading
free-form remittance advices.

The standard gate (run_evals.py) cannot measure that. Its pipeline
runs on the offline fallback, which reads the synthetic text layer
the dataset is written in; a perfect offline score says the CODE
is right, and says nothing about how a real model reads a messy
email from a broker's accounts desk. This pack is the missing
instrument. Its cases are deliberately messy free-form advices —
rambling preambles, comma amounts, a line with no policy reference
(only the numbered line is gold), a ragged table paying one policy
twice, an instruction injection that must be fenced by the screen
before any reader sees it — plus two lump sums: one only
explainable by reaching across two brokers' books (must be
refused: no proposal), and one with exactly one exact same-broker
explanation (must be proposed, to the cent).

Run it with a provider configured (the same env as the service —
OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL for any
OpenAI-compatible endpoint including NVIDIA NIM, ANTHROPIC_API_KEY,
or a local Ollama):

    make evals-live        # or: python evals/run_live_evals.py

With no provider configured the runner prints SKIPPED-LIVE and
exits 0. That is a deliberate non-result, never a pass: the set
has not measured anything until a real model has read the cases.
It is NOT part of `make evals`; the standard gate is unchanged.

Scoring: an advice case passes when every gold line is extracted
exactly (policy + amount, as a multiset) and nothing extra is
extracted; the reported accuracy is the share of gold lines
extracted exactly, with extras counted and named. A lump-sum case
passes when the proposal is exactly the gold allocation — or, for
the cross-broker case, when there is no proposal at all. As in the
pipeline, candidates are the payer-broker's own open items only
(the matcher's `_open_candidates` discipline): the cross-broker
fixture carries the other broker's item in the list the desk sees,
and the runner applies that discipline before the reader is
called, then also fails any proposal that names an item outside
the payer's book.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cash_recon import documents  # noqa: E402
from cash_recon.llm import Candidate, OfflineReader, ReaderError, make_reader  # noqa: E402

CASES_PATH = Path(__file__).parent / "live_cases.jsonl"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def score_advice_output(case: dict, extracted: list) -> tuple[bool, float, str]:
    """extracted: RemittanceLine-like objects (policy_number, amount)."""
    gold = Counter(
        (g["policy"], Decimal(g["amount"])) for g in case["gold_lines"]
    )
    got = Counter((line.policy_number, line.amount) for line in extracted)
    matched = sum((gold & got).values())
    extras = sum((got - gold).values())
    accuracy = matched / sum(gold.values()) if gold else 0.0
    passed = matched == sum(gold.values()) and extras == 0
    detail = (
        f"extracted {matched}/{sum(gold.values())} gold lines exactly"
        + (f"; {extras} extra line(s) extracted" if extras else "")
    )
    return passed, accuracy, detail


def score_lump_output(case: dict, proposal) -> tuple[bool, float, str]:
    own = {
        c["policy_number"] for c in case["candidates"]
        if c["broker_id"] == case["payer_broker_id"]
    }
    gold = case["gold_allocations"]
    if proposal is None:
        if gold is None:
            return True, 1.0, "no proposal — the wire is not explainable from the payer's own book, and none was forced"
        return False, 0.0, f"no proposal, but an exact explanation exists: {gold}"
    got = {item.policy_number: item.cash_due for item in proposal.allocations}
    outside = sorted(p for p in got if p not in own)
    if outside:
        return False, 0.0, f"proposal reaches outside the payer's book: {outside}"
    gold_dec = {p: Decimal(a) for p, a in (gold or {}).items()}
    if got == gold_dec:
        return True, 1.0, "proposal is exactly the gold allocation"
    return False, 0.0, f"proposal { {p: str(a) for p, a in got.items()} } != gold {gold}"


def run_cases(reader, cases: list[dict]) -> list[dict]:
    """Run every case against a reader port and score the outputs.
    The screen runs first, exactly as the pipeline runs it: the
    reader only ever sees the visible text."""
    results = []
    for case in cases:
        entry = {"case": case["case"], "kind": case["kind"]}
        try:
            if case["kind"] == "advice":
                visible, flagged, _hits = documents.screen_text(case["text"])
                if case.get("expect_injection_fenced") and not flagged:
                    entry.update(passed=False, accuracy=0.0,
                                 detail="the injection was NOT fenced by the screen")
                    results.append(entry)
                    continue
                lines = reader.read_remittance(visible)
                passed, accuracy, detail = score_advice_output(case, lines)
                if flagged:
                    detail += "; injection attempt fenced before reading"
                entry.update(passed=passed, accuracy=accuracy, detail=detail)
            elif case["kind"] == "lump_sum":
                own_candidates = [
                    Candidate(policy_number=c["policy_number"],
                              installment_id=c["installment_id"],
                              cash_due=Decimal(c["amount"]))
                    for c in case["candidates"]
                    if c["broker_id"] == case["payer_broker_id"]
                ]
                proposal = reader.propose_lump_sum(
                    Decimal(case["amount"]), case["payer"], own_candidates
                )
                passed, accuracy, detail = score_lump_output(case, proposal)
                entry.update(passed=passed, accuracy=accuracy, detail=detail)
            else:
                entry.update(passed=False, accuracy=0.0,
                             detail=f"unknown case kind {case['kind']!r}")
        except ReaderError as exc:
            entry.update(passed=False, accuracy=0.0, detail=f"reader refused: {exc}")
        results.append(entry)
    return results


def main() -> int:
    try:
        reader = make_reader()
    except RuntimeError as exc:
        print(f"LIVE EVALS NOT RUN — reader configuration error: {exc}")
        return 2
    if isinstance(reader, OfflineReader):
        print(
            "SKIPPED-LIVE: no model provider is configured, so the live "
            "remittance-reading set did not run. This is not a pass — the "
            "set measures a real model's reading and there is no model to "
            "measure. Configure a provider (OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "or OLLAMA_BASE_URL) and re-run `make evals-live`."
        )
        return 0
    cases = load_cases()
    print(f"Live remittance-reading evals — reader: {reader.name}, {len(cases)} cases")
    results = run_cases(reader, cases)
    failed = 0
    for entry in results:
        verdict = "PASS" if entry["passed"] else "FAIL"
        if not entry["passed"]:
            failed += 1
        print(f"  [{verdict}] {entry['case']}: accuracy {entry['accuracy']:.2f} — {entry['detail']}")
    print(f"LIVE GATE {'PASS' if not failed else 'FAIL'}: "
          f"{len(results) - failed}/{len(results)} cases")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
