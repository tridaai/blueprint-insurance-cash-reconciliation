"""Checkpoint retention: the LangGraph checkpoint store grows with
every run, and the governed prune cuts it back to the policy's
window — newest checkpoints kept per thread, a thread's latest
state always still loading, and nothing pruned that an operator
did not ask to prune.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from cash_recon import pipeline
from cash_recon.checkpoints import prune_checkpoints
from cash_recon.config import Policy as PolicyConfig
from cash_recon.dataset import build_dataset
from cash_recon.llm import OfflineReader
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import ingest_dataset, run_day
from cash_recon.store import InMemoryStore

DAYS = [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]
POLICY = PolicyConfig()


def _counts(path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute(
            "SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id"))
    finally:
        conn.close()


def _writes(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def checkpoint_db(tmp_path, monkeypatch):
    """Three day runs against a fresh checkpoint file (the store is
    in-memory; only the checkpointer touches the file, as in the
    local profile where both live in the one SQLite file)."""
    path = tmp_path / "checkpoints.db"
    monkeypatch.setenv("SQLITE_PATH", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    for day in DAYS:
        run_day(store, objects, OfflineReader(), POLICY, day, actor="cash-team")
    return path, store, objects


def test_prune_keeps_the_newest_n_per_thread_and_the_state_loads(checkpoint_db):
    path, store, objects = checkpoint_db
    before = _counts(path)
    assert len(before) == 3 and all(n >= 5 for n in before.values())
    writes_before = _writes(path)

    result = prune_checkpoints(
        PolicyConfig(checkpoint_keep_per_thread=2, checkpoint_max_age_days=30),
        sqlite_path=path)
    assert result["threads"] == 3
    assert result["checkpoints_pruned"] == sum(before.values()) - 6
    assert result["checkpoints_kept"] == 6
    assert result["writes_pruned"] > 0
    assert _counts(path) == {t: 2 for t in before}
    assert _writes(path) < writes_before

    # Every thread's newest state still loads, complete: the final
    # checkpoint holds the run's report.
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    for thread_id in before:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        assert tup is not None and tup.checkpoint["channel_values"].get("report")
    conn.close()

    # And the pruned store still runs: a fresh day completes on it.
    result_run = run_day(store, objects, OfflineReader(), POLICY,
                         date(2026, 9, 7), actor="cash-team")
    assert result_run["report"] is not None


def test_age_rule_prunes_inside_the_keep_window_but_never_the_newest(checkpoint_db):
    path, _store, _objects = checkpoint_db
    result = prune_checkpoints(
        PolicyConfig(checkpoint_keep_per_thread=50, checkpoint_max_age_days=0),
        sqlite_path=path)
    # Everything is older than zero days — yet each thread keeps its
    # single newest checkpoint, so its latest state always loads.
    assert result["checkpoints_kept"] == result["threads"] == 3
    assert _counts(path) == {t: 1 for t in _counts(path)}


def test_dry_run_counts_and_deletes_nothing(checkpoint_db):
    path, _store, _objects = checkpoint_db
    before, writes_before = _counts(path), _writes(path)
    result = prune_checkpoints(
        PolicyConfig(checkpoint_keep_per_thread=1, checkpoint_max_age_days=30),
        sqlite_path=path, dry_run=True)
    assert result["dry_run"] is True and result["checkpoints_pruned"] > 0
    assert _counts(path) == before and _writes(path) == writes_before


def test_prune_of_a_missing_database_is_a_note_not_an_error(tmp_path):
    result = prune_checkpoints(POLICY, sqlite_path=tmp_path / "never-existed.db")
    assert result["checkpoints_pruned"] == 0 and "nothing to prune" in result["note"]


def test_cli_prune_prints_the_counts(checkpoint_db, monkeypatch, capsys):
    from cash_recon import cli

    path, _store, _objects = checkpoint_db
    monkeypatch.setattr("sys.argv", ["cash-recon", "checkpoints", "prune", "--keep", "2"])
    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["checkpoints_kept"] == 6 and printed["profile"] == "sqlite"
    assert all(n == 2 for n in _counts(path).values())


def test_startup_prune_is_off_by_default_and_opt_in(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cash_recon.api import create_app

    path = tmp_path / "app.db"
    monkeypatch.setenv("SQLITE_PATH", str(path))
    monkeypatch.setenv("LLM_BACKEND", "offline")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CHECKPOINT_PRUNE_ON_STARTUP", raising=False)
    monkeypatch.setenv("CHECKPOINT_KEEP_PER_THREAD", "2")

    store, objects = InMemoryStore(), InMemoryObjectStore()
    ingest_dataset(store, objects, build_dataset())
    for day in DAYS[:2]:
        run_day(store, objects, OfflineReader(), POLICY, day, actor="cash-team")
    before = _counts(path)
    assert all(n > 2 for n in before.values())

    # Default: the API starts and the checkpoints are untouched.
    TestClient(create_app()).get("/readiness")
    assert _counts(path) == before

    # Opted in: one prune at startup, down to the policy's window.
    monkeypatch.setenv("CHECKPOINT_PRUNE_ON_STARTUP", "1")
    TestClient(create_app()).get("/readiness")
    assert all(n == 2 for n in _counts(path).values())


@pytest.mark.skipif(
    not os.environ.get("CASH_RECON_TEST_DATABASE_URL"),
    reason="set CASH_RECON_TEST_DATABASE_URL to run the Postgres prune",
)
def test_prune_postgres_keeps_newest_and_collects_blobs(monkeypatch):
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    base_dsn = os.environ["CASH_RECON_TEST_DATABASE_URL"]
    admin = psycopg.connect(base_dsn, autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS cashrecon_ckpt_prune")
    admin.execute("CREATE DATABASE cashrecon_ckpt_prune")
    admin.close()
    dsn = base_dsn.rsplit("/", 1)[0] + "/cashrecon_ckpt_prune"
    try:
        store, objects = InMemoryStore(), InMemoryObjectStore()
        ingest_dataset(store, objects, build_dataset())
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            monkeypatch.setattr(pipeline, "_checkpointer", lambda: saver)
            for day in DAYS[:2]:
                run_day(store, objects, OfflineReader(), POLICY, day, actor="cash-team")
            result = prune_checkpoints(
                PolicyConfig(checkpoint_keep_per_thread=2, checkpoint_max_age_days=30),
                dsn=dsn)
            assert result["profile"] == "postgres" and result["threads"] == 2
            assert result["checkpoints_kept"] == 4 and result["checkpoints_pruned"] > 0
            for day in DAYS[:2]:
                tup = saver.get_tuple(
                    {"configurable": {"thread_id": f"day-{day.isoformat()}-a1"}})
                assert tup is not None and tup.checkpoint["channel_values"].get("report")
    finally:
        admin = psycopg.connect(base_dsn, autocommit=True)
        admin.execute("DROP DATABASE IF EXISTS cashrecon_ckpt_prune WITH (FORCE)")
        admin.close()
