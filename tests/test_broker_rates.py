"""Broker commission rates have a history: a rate change mid-book
means the matcher prices a remittance by the period it belongs to,
not by whatever the rate is today. The gate's gross-up settles at
the same period rate, so what the matcher promised is what approval
posts."""

from datetime import date
from decimal import Decimal

from cash_recon.config import Policy as PolicyConfig
from cash_recon.gate import approve_pack, assemble_pack
from cash_recon.llm import OfflineReader
from cash_recon.matcher import MatchContext, match_line
from cash_recon.models import (
    BankLine,
    Broker,
    Installment,
    PolicyStatus,
    RatePeriod,
    Receivable,
)
from cash_recon.money import net_of
from cash_recon.store import InMemoryStore

D = Decimal

BROKER = Broker(
    broker_id="BRK-TEST", name="Testline Brokers", commission_rate=D("0.20"),
    rate_history=[
        RatePeriod(effective_from=date(2026, 1, 1), rate=D("0.10")),
        RatePeriod(effective_from=date(2026, 10, 15), rate=D("0.20")),
    ],
)


def _policy(number, premium):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id="BRK-TEST",
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id=f"{number}-I1", policy_number=number,
                                  sequence=1, due_date=date(2026, 9, 1), amount_due=premium)],
    )


def _ctx(recs):
    return MatchContext(
        receivables={r.policy_number: r for r in recs},
        brokers={"BRK-TEST": BROKER},
        remittances=[], prior_lines=[], settled_line_ids=set(),
        proposer=OfflineReader(), policy=PolicyConfig(),
    )


def _line(bid, day, amount, ref):
    return BankLine(bank_line_id=bid, value_date=day, amount=amount,
                    payer_name="Testline Brokers", reference=ref, narrative="")


def test_rate_on_picks_the_period_in_force():
    assert BROKER.rate_on(date(2026, 9, 30)) == D("0.10")
    assert BROKER.rate_on(date(2026, 10, 14)) == D("0.10")
    assert BROKER.rate_on(date(2026, 10, 15)) == D("0.20")
    assert BROKER.rate_on(date(2026, 12, 1)) == D("0.20")
    # Before every recorded period: the earliest period's rate.
    assert BROKER.rate_on(date(2025, 6, 1)) == D("0.10")
    # No history at all: the headline rate, any date.
    plain = Broker(broker_id="BRK-PLAIN", name="Plain Brokers", commission_rate=D("0.07"))
    assert plain.rate_on(date(2026, 10, 20)) == D("0.07")


def test_cash_before_the_change_nets_at_the_old_rate():
    rec = _policy("NSP-7001", D("1000.00"))
    out = match_line(_line("BL-X1", date(2026, 10, 6), net_of(D("1000.00"), D("0.10")), "NSP-7001"),
                     _ctx([rec]))
    assert out.tier.value == "T2_NETTING"
    assert out.allocations[0].amount == D("900.00")


def test_cash_after_the_change_nets_at_the_new_rate():
    rec = _policy("NSP-7002", D("1000.00"))
    out = match_line(_line("BL-X2", date(2026, 10, 20), net_of(D("1000.00"), D("0.20")), "NSP-7002"),
                     _ctx([rec]))
    assert out.tier.value == "T2_NETTING"
    assert out.allocations[0].amount == D("800.00")


def test_the_old_rate_no_longer_settles_after_the_change():
    # Same cash that settled cleanly before the change is, after it,
    # more than the net due at the rate now in force: the excess is
    # an overpayment in suspense, never a quiet T2 at a stale rate.
    rec = _policy("NSP-7003", D("1000.00"))
    out = match_line(_line("BL-X3", date(2026, 10, 20), net_of(D("1000.00"), D("0.10")), "NSP-7003"),
                     _ctx([rec]))
    assert out.tier is None or out.tier.value != "T2_NETTING"
    assert [e.code.value for e in out.exceptions] == ["OVERPAID"]
    assert out.allocations[0].amount == D("800.00")
    assert out.suspense_amount == D("100.00")


def test_the_gate_grosses_up_at_the_pack_days_rate():
    # A pack assembled from pre-change cash settles in full at the
    # old rate even when approval happens after the change: _apply's
    # rate comes from the pack's value date, and amount_applied
    # lands on the billed premium, not 900/0.8 of it.
    store = InMemoryStore()
    store.upsert_broker(BROKER)
    rec = _policy("NSP-7004", D("1000.00"))
    store.upsert_receivable(rec)
    out = match_line(_line("BL-X4", date(2026, 10, 6), net_of(D("1000.00"), D("0.10")), "NSP-7004"),
                     _ctx([rec]))
    pack = assemble_pack(date(2026, 10, 6), [out], "cash-team", PolicyConfig())
    store.save_pack(pack)
    approve_pack(store, pack.pack_id, "Priya Nair", PolicyConfig())
    settled = store.receivable("NSP-7004")
    assert settled.installments[0].amount_applied == D("1000.00")
    assert settled.status == PolicyStatus.PAID_IN_FULL
