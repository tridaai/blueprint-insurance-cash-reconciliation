"""Governed retention for the LangGraph checkpoint store.

Every day run appends a chain of checkpoints to the saver's tables
— one per graph node, holding the run's working state — and the
savers keep them all. Left alone the store grows without bound:
this repo's own scratch database passed 800 MB of checkpoints and
pending writes (over 125,000 checkpoints across the day threads)
from nothing but test and demo runs. Checkpoints are the graph's
scratch memory, not the books — the records live in the store
proper — so the tail is safe to cut, under a governed policy
(`Policy.checkpoint_keep_per_thread` /
`Policy.checkpoint_max_age_days`):

- per thread, the newest `checkpoint_keep_per_thread` checkpoints
  are kept;
- a kept checkpoint older than `checkpoint_max_age_days` is pruned
  too — except a thread's single newest checkpoint, which is never
  pruned, so the latest state of every thread always still loads
  and a graph can always be resumed or inspected where it stopped.

Pruning is an explicit operator act — `cash-recon checkpoints
prune` (with `--dry-run` to count without cutting) — never a
silent side effect. A deployment may opt into one prune at API
startup with `CHECKPOINT_PRUNE_ON_STARTUP=1`; the default is off,
because deleting anything is a decision, and decisions here are
named and on the record. The act reports what it did: threads
seen, checkpoints and writes before and after, and (on Postgres)
the orphaned channel blobs it collected — the Postgres saver keys
blobs by channel version rather than by checkpoint, so a blob is
deleted only when no surviving checkpoint of its thread names
that version.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Policy as PolicyConfig
from .store import store_profile

log = logging.getLogger("cash_recon.checkpoints")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _select_prune(rows: list[tuple[str, datetime | None]], keep: int,
                  cutoff: datetime | None) -> list[str]:
    """rows: (checkpoint_id, ts) newest first for one thread. Returns
    the ids to prune: everything beyond the newest `keep`, plus kept
    checkpoints older than the cutoff — never the thread's newest."""
    doomed = [cid for cid, _ts in rows[keep:]]
    if cutoff is not None:
        for cid, ts in rows[1:keep]:
            if ts is not None and ts < cutoff:
                doomed.append(cid)
    return doomed


def prune_checkpoints(policy: PolicyConfig, *, sqlite_path: str | Path | None = None,
                      dsn: str | None = None, dry_run: bool = False,
                      now: datetime | None = None) -> dict:
    """Prune the deployment's checkpoint store to the policy's
    retention window. The target follows the store profile (the
    same one decision as everything else) unless a path or DSN is
    given explicitly. Returns the counts, whether it cut or only
    counted (dry_run)."""
    if sqlite_path is not None:
        kind, target = "sqlite", str(sqlite_path)
    elif dsn is not None:
        kind, target = "postgres", dsn
    else:
        kind, target = store_profile()
    now = now or datetime.now(timezone.utc)
    cutoff = (
        now - timedelta(days=policy.checkpoint_max_age_days)
        if policy.checkpoint_max_age_days is not None
        else None
    )
    if kind == "postgres":
        return _prune_postgres(target, policy, cutoff, dry_run)
    return _prune_sqlite(Path(target), policy, cutoff, dry_run)


def _result(profile: str, **kw) -> dict:
    base = {
        "profile": profile,
        "threads": 0,
        "checkpoints_before": 0,
        "checkpoints_kept": 0,
        "checkpoints_pruned": 0,
        "writes_pruned": 0,
        "blobs_pruned": 0,
        "dry_run": False,
        "note": "",
    }
    base.update(kw)
    return base


def _prune_sqlite(path: Path, policy: PolicyConfig, cutoff, dry_run: bool) -> dict:
    if not path.exists():
        return _result("sqlite", dry_run=dry_run,
                       note=f"no checkpoint database at {path}; nothing to prune")
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "checkpoints" not in tables:
            return _result("sqlite", dry_run=dry_run,
                           note="the database holds no checkpoint tables; nothing to prune")
        rows = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints "
            "ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC"
        ).fetchall()
        writes_before = (
            conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
            if "writes" in tables else 0
        )
        threads: dict[tuple[str, str], list[str]] = {}
        for thread_id, ns, cid in rows:
            threads.setdefault((thread_id, ns), []).append(cid)

        # Timestamps live inside the saver's serialised payloads, so
        # the age pass reads them back through the saver itself —
        # bounded by the keep window per thread, never the whole
        # table.
        from langgraph.checkpoint.sqlite import SqliteSaver

        saver = SqliteSaver(conn)
        doomed: list[tuple[str, str, str]] = []
        for (thread_id, ns), ids in threads.items():
            stamps: dict[str, datetime | None] = {}
            if cutoff is not None and len(ids) > 1:
                config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
                for tup in saver.list(config, limit=policy.checkpoint_keep_per_thread):
                    cid = tup.config["configurable"].get("checkpoint_id")
                    if cid:
                        stamps[cid] = _parse_ts(tup.checkpoint.get("ts"))
            ordered = [(cid, stamps.get(cid)) for cid in ids]
            doomed.extend(
                (thread_id, ns, cid)
                for cid in _select_prune(ordered, policy.checkpoint_keep_per_thread, cutoff)
            )
        writes_pruned = 0
        if doomed and "writes" in tables:
            marks = ",".join("?" for _ in {c for _t, _n, c in doomed})
            writes_pruned = conn.execute(
                f"SELECT COUNT(*) FROM writes WHERE checkpoint_id IN ({marks})",
                tuple({c for _t, _n, c in doomed}),
            ).fetchone()[0]
        if doomed and not dry_run:
            conn.executemany(
                "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                doomed,
            )
            if "writes" in tables:
                conn.executemany(
                    "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? "
                    "AND checkpoint_id = ?",
                    doomed,
                )
            conn.commit()
        kept = len(rows) - len(doomed)
        return _result(
            "sqlite",
            threads=len(threads),
            checkpoints_before=len(rows),
            checkpoints_kept=kept,
            checkpoints_pruned=len(doomed),
            writes_pruned=writes_pruned,
            dry_run=dry_run,
            note=("counted only (dry run); nothing was deleted"
                  if dry_run else
                  "the file itself shrinks only on VACUUM; the rows are gone either way"),
        )
    finally:
        conn.close()


def _prune_postgres(dsn: str, policy: PolicyConfig, cutoff, dry_run: bool) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        tables = {r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        if "checkpoints" not in tables:
            return _result("postgres", dry_run=dry_run,
                           note="the database holds no checkpoint tables; nothing to prune")
        rows = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, checkpoint ->> 'ts' AS ts "
            "FROM checkpoints ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC"
        ).fetchall()
        threads: dict[tuple[str, str], list[tuple[str, datetime | None]]] = {}
        for r in rows:
            threads.setdefault((r["thread_id"], r["checkpoint_ns"]), []).append(
                (r["checkpoint_id"], _parse_ts(r["ts"])))
        doomed: list[tuple[str, str, str]] = []
        for (thread_id, ns), ordered in threads.items():
            doomed.extend(
                (thread_id, ns, cid)
                for cid in _select_prune(ordered, policy.checkpoint_keep_per_thread, cutoff)
            )
        writes_pruned = blobs_pruned = 0
        if doomed and not dry_run:
            for thread_id, ns, cid in doomed:
                conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s "
                    "AND checkpoint_id = %s",
                    (thread_id, ns, cid),
                )
                cur = conn.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s "
                    "AND checkpoint_ns = %s AND checkpoint_id = %s",
                    (thread_id, ns, cid),
                )
                writes_pruned += cur.rowcount or 0
            # Blob collection: a blob is dead when no surviving
            # checkpoint of its thread names its channel version.
            affected = {(t, n) for t, n, _c in doomed}
            for thread_id, ns in affected:
                live: set[tuple[str, str]] = set()
                for r in conn.execute(
                    "SELECT checkpoint -> 'channel_versions' AS cv FROM checkpoints "
                    "WHERE thread_id = %s AND checkpoint_ns = %s",
                    (thread_id, ns),
                ):
                    for channel, version in (r["cv"] or {}).items():
                        live.add((channel, str(version)))
                for r in conn.execute(
                    "SELECT channel, version FROM checkpoint_blobs "
                    "WHERE thread_id = %s AND checkpoint_ns = %s",
                    (thread_id, ns),
                ):
                    if (r["channel"], str(r["version"])) not in live:
                        cur = conn.execute(
                            "DELETE FROM checkpoint_blobs WHERE thread_id = %s "
                            "AND checkpoint_ns = %s AND channel = %s AND version = %s",
                            (thread_id, ns, r["channel"], r["version"]),
                        )
                        blobs_pruned += cur.rowcount or 0
            conn.commit()
        elif doomed:
            ids = [c for _t, _n, c in doomed]
            writes_pruned = conn.execute(
                "SELECT COUNT(*) AS n FROM checkpoint_writes WHERE checkpoint_id = ANY(%s)",
                (ids,),
            ).fetchone()["n"]
        return _result(
            "postgres",
            threads=len(threads),
            checkpoints_before=len(rows),
            checkpoints_kept=len(rows) - len(doomed),
            checkpoints_pruned=len(doomed),
            writes_pruned=writes_pruned,
            blobs_pruned=blobs_pruned,
            dry_run=dry_run,
            note=("counted only (dry run)" if dry_run else ""),
        )
