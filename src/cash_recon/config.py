"""Deployment policy: thresholds a carrier sets once, in one place.

Nothing in this module is a match decision. It holds the numbers the
code paths consult — the auto-apply ceiling, the write-off limit that
triggers a second approver, the age at which unapplied cash counts as
stale, and the netting tolerance (one cent, because bank money is
exact and a wider tolerance would hide real deltas).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Policy:
    auto_apply_threshold: Decimal = Decimal("500.00")
    write_off_single_limit: Decimal = Decimal("250.00")
    stale_suspense_days: int = 30
    netting_tolerance: Decimal = Decimal("0.01")
    duplicate_window_days: int = 5

    @classmethod
    def from_env(cls) -> "Policy":
        def dec(name, default):
            raw = os.environ.get(name)
            return Decimal(raw) if raw else default

        def num(name, default):
            raw = os.environ.get(name)
            return int(raw) if raw else default

        return cls(
            auto_apply_threshold=dec("AUTO_APPLY_THRESHOLD", cls.auto_apply_threshold),
            write_off_single_limit=dec(
                "WRITE_OFF_SINGLE_LIMIT", cls.write_off_single_limit
            ),
            stale_suspense_days=num("STALE_SUSPENSE_DAYS", cls.stale_suspense_days),
            netting_tolerance=dec("NETTING_TOLERANCE", cls.netting_tolerance),
            duplicate_window_days=num(
                "DUPLICATE_WINDOW_DAYS", cls.duplicate_window_days
            ),
        )


# ---------------------------------------------------------------------------
# Non-premium classification: bank lines that are not premium cash.
#
# Real statements carry lines that are not premium at all — the bank's
# own fee rebate, an interest credit on the premium trust account, a
# transfer between the carrier's own accounts. Left to the tiers they
# land in UNIDENTIFIED suspense and pollute the aging auditors watch.
# The classifier (nonpremium.py) is code-first and conservative: a
# line is classified only when it matches a rule below EXACTLY —
# every field a rule names must match. `payer_exact` compares the
# whole payer name after case/whitespace normalisation: a payer that
# merely resembles the carrier ("Northgate Builders LLC") is not the
# carrier, and a bank line whose narrative does not name the rule's
# text is not classified either. Anything ambiguous stays
# UNIDENTIFIED suspense, exactly as before. The model never
# classifies these lines.
#
# A rule matches when ALL of its named fields match:
#   payer_exact        the payer name, exactly (normalised)
#   narrative_contains a substring of the line's narrative (lowercase)
#   reference_prefix   the line's reference starts with this
NON_PREMIUM_RULES: tuple[dict, ...] = (
    {
        "kind": "OWN_TRANSFER",
        "payer_exact": "Northgate Specialty Insurance",
    },
    {
        "kind": "INTEREST",
        "payer_exact": "Northgate Bank",
        "narrative_contains": "interest",
    },
    {
        "kind": "BANK_FEE",
        "payer_exact": "Northgate Bank",
        "narrative_contains": "fee",
    },
)


# ---------------------------------------------------------------------------
# Foreign exchange: the governed rate table.
#
# Northgate's functional currency is USD. Premium is also billed and
# collected in EUR, and the cash desk converts at governed rates —
# never at a rate the model suggests, never at a spot rate fetched
# mid-match. A rate is quoted as functional units per one unit of
# the foreign currency (EUR 1.08 = one euro is 1.08 dollars), with
# an effective-from date, in the same mould as a broker's commission
# rate history: a period prices by the date in force, and when a
# rate changes mid-period the business on either side of the change
# is priced at its own date's rate. Conversion happens in code
# (fx.py), at defined points only: when an allocation is created
# (at the rate in force on the bank line's value date) and when a
# report translates a position for presentation (at the rate it
# names). A recorded conversion is never re-priced.
FX_RATE_HISTORY: dict[str, tuple[tuple[date, Decimal], ...]] = {
    "EUR": (
        (date(2026, 1, 1), Decimal("1.08")),
        (date(2026, 11, 16), Decimal("1.10")),
    ),
}


@dataclass(frozen=True)
class WatchPolicy:
    """The match-quality watch's tolerances, in the same one place.

    The trend watch (trends.py) names a deterioration only when a
    movement crosses one of these lines: the auto-applied rate
    falling by more than `auto_rate_drop_points` month over month,
    an exception code's share of bank value multiplying by
    `exception_share_doubling_factor` (and reaching at least
    `exception_share_floor`, so a code doubling from nothing to
    nearly nothing is not a finding), or average suspense dwell
    rising by more than `suspense_dwell_rise_days`. Inside the
    lines, the watch says "within tolerance" — explicitly, with
    the figures — rather than staying silent.
    """

    auto_rate_drop_points: Decimal = Decimal("0.05")
    exception_share_doubling_factor: Decimal = Decimal("2")
    exception_share_floor: Decimal = Decimal("0.01")
    suspense_dwell_rise_days: Decimal = Decimal("2")

    @classmethod
    def from_env(cls) -> "WatchPolicy":
        def dec(name, default):
            raw = os.environ.get(name)
            return Decimal(raw) if raw else default

        return cls(
            auto_rate_drop_points=dec(
                "WATCH_AUTO_RATE_DROP_POINTS", cls.auto_rate_drop_points
            ),
            exception_share_doubling_factor=dec(
                "WATCH_EXCEPTION_SHARE_DOUBLING_FACTOR",
                cls.exception_share_doubling_factor,
            ),
            exception_share_floor=dec(
                "WATCH_EXCEPTION_SHARE_FLOOR", cls.exception_share_floor
            ),
            suspense_dwell_rise_days=dec(
                "WATCH_SUSPENSE_DWELL_RISE_DAYS", cls.suspense_dwell_rise_days
            ),
        )
