"""Foreign exchange: governed rates, conversion in code.

Northgate's functional currency is USD, but premium is billed and
collected in more than one currency. The rules, enforced here and
in the matcher:

- Matching is IN CURRENCY. A bank line is worked against open items
  denominated in the line's own currency — exact, netting, and
  proposals alike. The broker's commission rate applies in that
  currency; no rate in this module is ever consulted to decide a
  match, and cross-currency netting is refused by design.
- Conversion is a RECORDING act, not a matching act. When an
  allocation is created for a non-functional line, the functional
  amount is computed here — at the governed rate in force on the
  bank line's value date, rounded half-up to cents at that one
  point — and recorded on the allocation with the rate used. It is
  never recomputed afterwards: a later rate move does not restate
  history, and a return reversing the allocation replays the
  recorded conversion, not today's rate.
- Reports translate for presentation only. A day report's or close
  pack's functional figures name the rates they used, and the close
  pack labels its month-end translations as translations, not
  postings. The controls — footing, the suspense identity — always
  run per currency, in the currency itself.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import FX_RATE_HISTORY
from .money import cents

FUNCTIONAL_CURRENCY = "USD"


def rate_on(currency: str, as_of: date) -> Decimal:
    """The governed rate in force on `as_of`: functional units per
    one unit of `currency`. The functional currency itself prices at
    exactly 1. A currency with no table is a configuration error —
    refusing loudly beats converting at an invented rate."""
    if currency == FUNCTIONAL_CURRENCY:
        return Decimal("1")
    periods = FX_RATE_HISTORY.get(currency)
    if not periods:
        raise ValueError(
            f"no governed FX rate table for {currency}: add it to "
            "FX_RATE_HISTORY in config.py before accepting cash in it"
        )
    in_force = [rate for start, rate in periods if start <= as_of]
    return in_force[-1] if in_force else periods[0][1]


def to_functional(amount: Decimal, currency: str, as_of: date) -> Decimal:
    """`amount` converted to the functional currency at the rate in
    force on `as_of`, rounded half-up to cents — the one defined
    rounding point for a conversion."""
    if currency == FUNCTIONAL_CURRENCY:
        return cents(amount)
    return cents(amount * rate_on(currency, as_of))


def currencies_on_the_books() -> list[str]:
    """Every currency the governed table knows, functional first."""
    return [FUNCTIONAL_CURRENCY, *sorted(FX_RATE_HISTORY)]
