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
