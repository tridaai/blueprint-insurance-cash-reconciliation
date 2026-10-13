-- Return premiums — SQLite dialect. Same tables and semantics as
-- migrations/0004_return_premiums.sql (PostgreSQL); dates and
-- payloads are TEXT, as everywhere in this dialect.
CREATE TABLE IF NOT EXISTS cancellations (
    cancellation_id TEXT PRIMARY KEY,
    policy_number   TEXT NOT NULL,
    effective_date  TEXT NOT NULL,
    recorded_on     TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS return_premium_payables (
    payable_id    TEXT PRIMARY KEY,
    policy_number TEXT NOT NULL,
    status        TEXT NOT NULL,
    opened_on     TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payout_packs (
    pack_id    TEXT PRIMARY KEY,
    value_date TEXT NOT NULL,
    status     TEXT NOT NULL,
    payload       TEXT NOT NULL
);
