"""Money handling: Decimal end to end, cents exact.

One rounding rule for the whole blueprint: half-up, to cents, applied
only where a computation genuinely produces a fraction of a cent
(commission on a premium). Everywhere else amounts are already cents
and any sub-cent value is a bug, not something to round away quietly.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def cents(value) -> Decimal:
    """Coerce an int/str/Decimal to a cents-exact Decimal (half-up)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def commission_on(premium: Decimal, rate: Decimal) -> Decimal:
    """Broker commission for a premium at a contracted rate.

    This is the one defined rounding point in the netting math:
    premium * rate, half-up to cents.
    """
    return cents(premium * rate)


def net_of(premium: Decimal, rate: Decimal) -> Decimal:
    """What a broker is expected to remit for a premium: gross minus
    the contracted commission, to the cent."""
    return cents(premium) - commission_on(premium, rate)


def as_str(amount: Decimal) -> str:
    """Stable two-decimal rendering for records, reports and JSON."""
    return str(cents(amount))
