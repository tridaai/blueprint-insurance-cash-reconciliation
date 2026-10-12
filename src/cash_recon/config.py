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
