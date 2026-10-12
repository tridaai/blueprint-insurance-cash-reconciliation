"""The model port: the offline fallback reads the text layer, and a
connected model's output is believed only after in-code validation —
citations must be real document text, proposals must foot."""

import json
from decimal import Decimal

import pytest

from cash_recon.llm import Candidate, LLMReader, OfflineReader, ReaderError

D = Decimal

ADVICE = (
    "Remittance advice — Alder & Voss Insurance Brokers\n"
    "Policy NSP-1002 — Pinecrest Builders LLC — 10,850.00\n"
    "Policy NSP-1011 — Sable Courier Services — 6,431.60\n"
    "Total remitted: 17,281.60"
)


def test_offline_reader_extracts_lines_and_total():
    reader = OfflineReader()
    lines = reader.read_remittance(ADVICE)
    assert {(l.policy_number, l.amount) for l in lines} == {
        ("NSP-1002", D("10850.00")), ("NSP-1011", D("6431.60")),
    }
    assert all(l.citation in ADVICE for l in lines)
    assert reader.stated_total(ADVICE) == D("17281.60")


def test_llm_reader_accepts_valid_cited_output():
    def chat(messages):
        return json.dumps({"lines": [
            {"policy_number": "NSP-1002", "amount": "10850.00",
             "citation": "Policy NSP-1002 — Pinecrest Builders LLC — 10,850.00"},
        ]})

    lines = LLMReader(chat, "stub").read_remittance(ADVICE)
    assert len(lines) == 1 and lines[0].amount == D("10850.00")


def test_llm_reader_rejects_a_citation_that_is_not_in_the_document():
    calls = {"n": 0}

    def chat(messages):
        calls["n"] += 1
        return json.dumps({"lines": [
            {"policy_number": "NSP-1002", "amount": "10850.00", "citation": "a line I invented"},
        ]})

    with pytest.raises(ReaderError):
        LLMReader(chat, "stub").read_remittance(ADVICE)
    assert calls["n"] == 2  # one bounded retry, then refusal


def test_llm_proposal_must_foot_and_use_known_items():
    candidates = [
        Candidate(policy_number="NSP-1001", installment_id="NSP-1001-I1", cash_due=D("100.00")),
        Candidate(policy_number="NSP-1002", installment_id="NSP-1002-I1", cash_due=D("50.00")),
    ]

    def not_footing(messages):
        return json.dumps({"allocations": [
            {"policy_number": "NSP-1001", "installment_id": "NSP-1001-I1", "amount": "90.00"},
        ], "reasoning": "close", "rejected": []})

    assert LLMReader(not_footing, "stub").propose_lump_sum(D("150.00"), "Broker", candidates) is None

    def invented(messages):
        return json.dumps({"allocations": [
            {"policy_number": "NSP-9999", "installment_id": "NSP-9999-I1", "amount": "150.00"},
        ], "reasoning": "made up", "rejected": []})

    assert LLMReader(invented, "stub").propose_lump_sum(D("150.00"), "Broker", candidates) is None

    def exact(messages):
        return json.dumps({"allocations": [
            {"policy_number": "NSP-1001", "installment_id": "NSP-1001-I1", "amount": "100.00"},
            {"policy_number": "NSP-1002", "installment_id": "NSP-1002-I1", "amount": "50.00"},
        ], "reasoning": "the two open items", "rejected": ["single-item readings"]})

    proposal = LLMReader(exact, "stub").propose_lump_sum(D("150.00"), "Broker", candidates)
    assert proposal is not None and len(proposal.allocations) == 2
    assert proposal.rejected == ["single-item readings"]


def test_offline_proposer_finds_the_exact_combination():
    reader = OfflineReader()
    candidates = [
        Candidate(policy_number="NSP-1", installment_id="a", cash_due=D("40.00")),
        Candidate(policy_number="NSP-2", installment_id="b", cash_due=D("60.00")),
        Candidate(policy_number="NSP-3", installment_id="c", cash_due=D("25.00")),
    ]
    proposal = reader.propose_lump_sum(D("100.00"), "Broker", candidates)
    assert proposal is not None and len(proposal.allocations) == 2
    assert reader.propose_lump_sum(D("99.99"), "Broker", candidates) is None


def test_broker_note_carries_the_figures():
    note = OfflineReader().draft_broker_note("Copperline", "NSP-1004", D("5372.00"), D("5182.40"))
    assert "5372.00" in note and "5182.40" in note and "189.60" in note
