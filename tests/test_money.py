"""Netting math: Decimal end to end, half-up at the one defined point."""

from decimal import Decimal

from cash_recon.money import cents, commission_on, net_of

D = Decimal


def test_commission_rounds_half_up_to_cents():
    # 5215.25 * 10% = 521.525 -> 521.53 (half-up, not banker's)
    assert commission_on(D("5215.25"), D("0.10")) == D("521.53")
    # 10475 * 12.5% = 1309.375 -> 1309.38
    assert commission_on(D("10475.00"), D("0.125")) == D("1309.38")
    # 2468.87 * 10% = 246.887 -> 246.89
    assert commission_on(D("2468.87"), D("0.10")) == D("246.89")


def test_net_of_is_gross_minus_rounded_commission():
    assert net_of(D("5215.25"), D("0.10")) == D("4693.72")
    assert net_of(D("21000.00"), D("0.075")) == D("19425.00")
    assert net_of(D("6320.00"), D("0.15")) == D("5372.00")


def test_cents_coerces_without_float_error():
    assert cents("0.1") == D("0.10")
    assert cents(7) == D("7.00")
    assert cents(D("1.005")) == D("1.01")
