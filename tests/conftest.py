"""Shared fixtures: a seeded in-memory world and a day runner that
approves each pack as a named controller, like the desk would."""

from __future__ import annotations

from datetime import date

import pytest

from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.gate import approve_pack
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import ingest_dataset, run_day
from cash_recon.store import InMemoryStore

CONTROLLER = "Priya Nair"
ASSEMBLER = "cash-team"


@pytest.fixture()
def world():
    ds = build_dataset()
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, ds)
    return ds, store, objects


@pytest.fixture()
def policy():
    return PolicyConfig()


def run_and_approve(store, objects, day, policy=None):
    result = run_day(store, objects, OfflineReader(), policy or PolicyConfig(),
                     day, actor=ASSEMBLER)
    if result["pack_id"]:
        approve_pack(store, result["pack_id"], CONTROLLER, policy or PolicyConfig())
    return result


def run_through(store, objects, last_day, policy=None):
    """Run every dataset day up to last_day, approving as we go."""
    days = sorted(
        {l.value_date for l in store.bank_lines()} | {r.received_date for r in store.remittances()}
    )
    results = {}
    for day in days:
        if day < date(2026, 9, 1) or day > last_day:
            continue
        results[day] = run_and_approve(store, objects, day, policy)
    return results
