"""Cash for a cancelled policy meets the return premium owed on it
first: the netting is proposed by the matcher, lands only when the
pack is approved, and the day foots with the netting as its own
term. A lump-sum proposal that spans two brokers' books is refused
whole — cross-broker allocation is not a settlement."""

from datetime import date
from decimal import Decimal

from cash_recon.cancellations import record_cancellation
from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import approve_pack
from cash_recon.llm import Candidate, OfflineReader, Proposal
from cash_recon.matcher import MatchContext, match_line
from cash_recon.models import (
    BankLine,
    Broker,
    Installment,
    PolicyStatus,
    Receivable,
    Remittance,
)
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.report import day_report
from cash_recon.store import InMemoryStore

D = Decimal
CONTROLLER = "Priya Nair"
POLICY = PolicyConfig()


def _policy(number, premium, broker_id=None, term=(date(2026, 9, 1), date(2027, 9, 1))):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I{i + 1}", policy_number=number,
                                   sequence=i + 1, due_date=date(2026, 9, 1),
                                   amount_due=premium / 4)
                      for i in range(4)],
        term_start=term[0], term_end=term[1],
    )


def _cancelled_world():
    """NSP-6001 paid 6,000.00 of 12,000.00, then cancelled on
    2026-12-01: a 3,008.22 payable stands open to the insured."""
    store, objects = InMemoryStore(), InMemoryObjectStore()
    store.upsert_receivable(_policy("NSP-6001", D("12000.00")))
    store.add_bank_line(BankLine(bank_line_id="BL-N1", value_date=date(2026, 9, 2),
                                 amount=D("6000.00"), payer_name="Insured NSP-6001",
                                 reference="NSP-6001", narrative=""))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 9, 2), actor="cash-team")
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    record_cancellation(store, "NSP-6001", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    return store, objects


def test_a_wire_for_the_cancelled_policy_nets_against_the_payable():
    store, objects = _cancelled_world()
    store.add_bank_line(BankLine(bank_line_id="BL-N2", value_date=date(2026, 12, 3),
                                 amount=D("3000.00"), payer_name="Insured NSP-6001",
                                 reference="NSP-6001", narrative="installment 3"))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 12, 3), actor="cash-team")
    raw = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-N2")
    assert raw["allocations"] == [] and raw["suspense_amount"] == D("0.00")
    assert raw["exceptions"] == []
    assert [(o["payable_id"], o["amount"]) for o in raw["offsets"]] == [("PAY-NSP-6001", D("3000.00"))]
    # The payable has not moved yet: the offset lands at approval.
    assert store.payable("PAY-NSP-6001").remaining == D("3008.22")
    pack = store.pack(result["pack_id"])
    assert [e.kind for e in pack.entries] == ["payable_offset"]
    assert result["report"]["netted_total"] == "3000.00"
    assert result["report"]["footing_ok"] is True
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    payable = store.payable("PAY-NSP-6001")
    assert payable.remaining == D("8.22") and payable.status == "open"
    assert [(e.kind, e.ref) for e in payable.events] == [("opened", "CXL-NSP-6001"),
                                                         ("netted", "BL-N2")]


def test_a_wire_bigger_than_the_payable_nets_what_it_can():
    store, objects = _cancelled_world()
    store.add_bank_line(BankLine(bank_line_id="BL-N3", value_date=date(2026, 12, 3),
                                 amount=D("4000.00"), payer_name="Insured NSP-6001",
                                 reference="NSP-6001", narrative=""))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 12, 3), actor="cash-team")
    raw = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-N3")
    assert [(o["payable_id"], o["amount"]) for o in raw["offsets"]] == [("PAY-NSP-6001", D("3008.22"))]
    assert [e["code"] for e in raw["exceptions"]] == ["CANCELLED_POLICY"]
    assert raw["exceptions"][0]["amount"] == D("991.78")
    assert raw["suspense_amount"] == D("991.78")
    assert result["report"]["footing_ok"] is True
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    payable = store.payable("PAY-NSP-6001")
    assert payable.remaining == D("0.00") and payable.status == "settled"


def test_two_wires_cannot_net_more_than_the_payable():
    store, objects = _cancelled_world()
    for bid in ("BL-N4", "BL-N5"):
        store.add_bank_line(BankLine(bank_line_id=bid, value_date=date(2026, 12, 3),
                                     amount=D("3000.00"), payer_name="Insured NSP-6001",
                                     reference="NSP-6001", narrative=""))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 12, 3), actor="cash-team")
    total_offsets = sum(
        (o["amount"] for raw in result["outcomes"] for o in raw["offsets"]), D("0")
    )
    assert total_offsets == D("3008.22")  # the second wire nets only the 8.22 standing
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    assert store.payable("PAY-NSP-6001").status == "settled"


def test_a_remittance_line_for_the_cancelled_policy_nets_too():
    broker = Broker(broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.10"))
    store, objects = InMemoryStore(), InMemoryObjectStore()
    store.upsert_broker(broker)
    rec6002 = _policy("NSP-6002", D("12000.00"), broker_id="BRK-TEST")
    rec6002.insured_name = "Harbour and Sons Chandlery"
    store.upsert_receivable(rec6002)
    store.add_bank_line(BankLine(bank_line_id="BL-N6", value_date=date(2026, 9, 2),
                                 amount=D("5400.00"), payer_name="Testline Brokers",
                                 reference="NSP-6002", narrative=""))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 9, 2), actor="cash-team")
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    # Paid to date on the billed basis is 6,000.00 (5,400 net at 10%).
    cxl = record_cancellation(store, "NSP-6002", date(2026, 12, 1), CONTROLLER, date(2026, 12, 1))
    assert cxl.return_premium == D("3008.22")
    advice = "\n".join([
        "Remittance advice — Testline Brokers",
        "Broker account: BRK-TEST",
        "Date: 2026-12-03",
        "",
        "Premium received for the following policies (net of commission):",
        "Policy NSP-6002, Harbour and Sons Chandlery — 3,500.00",
        "",
        "Total remitted: 3,500.00",
    ])
    store.add_remittance(Remittance(remittance_id="REM-N1", broker_id="BRK-TEST",
                                    received_date=date(2026, 12, 3), stated_total=None,
                                    text=advice))
    store.add_bank_line(BankLine(bank_line_id="BL-N7", value_date=date(2026, 12, 3),
                                 amount=D("3500.00"), payer_name="Testline Brokers",
                                 reference=None, narrative="wire — see remittance advice"))
    result = run_day(store, objects, OfflineReader(), POLICY, date(2026, 12, 3), actor="cash-team")
    raw = next(o for o in result["outcomes"] if o["bank_line_id"] == "BL-N7")
    assert [(o["payable_id"], o["amount"]) for o in raw["offsets"]] == [("PAY-NSP-6002", D("3008.22"))]
    assert [e["code"] for e in raw["exceptions"]] == ["CANCELLED_POLICY"]
    assert raw["suspense_amount"] == D("491.78")
    approve_pack(store, result["pack_id"], CONTROLLER, POLICY)
    assert store.payable("PAY-NSP-6002").status == "settled"
    rep = day_report(store, date(2026, 12, 3), POLICY)
    assert rep["netted_total"] == "3008.22" and rep["footing_ok"] is True
    assert rep["return_premiums"]["netted_total"] == "3008.22"


class _CrossBrokerProposer:
    """A proposer that (wrongly) settles one broker's wire with two
    brokers' open items — it even fabricates the other broker's
    candidate, which was never in the list it was given. The
    combination foots, and it must still be refused whole."""

    name = "stub:cross-broker"

    def propose_lump_sum(self, amount, payer, candidates):
        own = next(c for c in candidates if c.policy_number == "NSP-8001")
        foreign = Candidate(
            installment_id="NSP-8002-I1", policy_number="NSP-8002",
            insured_name="Insured NSP-8002", gross_due=D("2000.00"),
            cash_due=D("1800.00"), amount_to_apply=D("1800.00"),
        )
        return Proposal(allocations=[own, foreign], reasoning="foots, across brokers",
                        backend=self.name)

    def read_remittance(self, text):
        return []

    def draft_broker_note(self, *args):
        return ""


def test_a_cross_broker_proposal_is_refused_whole():
    alder = Broker(broker_id="BRK-A", name="Aaa Brokers", commission_rate=D("0.10"))
    bay = Broker(broker_id="BRK-B", name="Bbb Brokers", commission_rate=D("0.10"))

    def rec(number, broker_id, gross):
        return Receivable(
            policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
            status=PolicyStatus.ACTIVE,
            installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                       sequence=1, due_date=date(2026, 10, 1),
                                       amount_due=gross)])

    recs = [rec("NSP-8001", "BRK-A", D("1000.00")),   # net 900.00
            rec("NSP-8003", "BRK-A", D("500.00")),    # net 450.00
            rec("NSP-8002", "BRK-B", D("2000.00"))]   # net 1800.00
    ctx = MatchContext(
        receivables={r.policy_number: r for r in recs},
        brokers={"BRK-A": alder, "BRK-B": bay},
        remittances=[], prior_lines=[], settled_line_ids=set(),
        proposer=_CrossBrokerProposer(), policy=PolicyConfig(),
    )
    # 2,700.00 = NSP-8001's net (900) + NSP-8002's net (1800). No
    # subset of BRK-A's own book (900, 450, 1350) foots it, so only
    # the rigged cross-broker proposal could settle the wire — and
    # the matcher refuses it whole.
    line = BankLine(bank_line_id="BL-XB", value_date=date(2026, 10, 13),
                    amount=D("2700.00"), payer_name="Aaa Brokers", reference=None,
                    narrative="monthly settlement")
    out = match_line(line, ctx)
    assert out.allocations == []
    assert [e.code.value for e in out.exceptions] == ["UNIDENTIFIED"]
    assert out.suspense_amount == D("2700.00")
