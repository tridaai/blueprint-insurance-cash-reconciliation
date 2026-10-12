-- Core records for premium cash reconciliation — SQLite dialect.
-- Same tables, columns and semantics as migrations/0001_core.sql
-- (PostgreSQL); only the dialect differs. Entity payloads are JSON
-- text (amounts as decimal strings inside them, exactly as in the
-- JSONB story), and money in key columns is TEXT holding a decimal
-- string — never REAL, so a cent that goes in comes back exact.
CREATE TABLE IF NOT EXISTS brokers (
    broker_id TEXT PRIMARY KEY,
    payload   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receivables (
    policy_number TEXT PRIMARY KEY,
    broker_id     TEXT,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_lines (
    bank_line_id TEXT PRIMARY KEY,
    value_date   TEXT NOT NULL,
    amount       TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remittances (
    remittance_id TEXT PRIMARY KEY,
    received_date TEXT NOT NULL,
    text_hash     TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    bank_line_id   TEXT NOT NULL,
    policy_number  TEXT NOT NULL,
    installment_id TEXT NOT NULL,
    amount         TEXT NOT NULL,
    tier           TEXT,
    pack_id        TEXT,
    payload        TEXT NOT NULL,
    PRIMARY KEY (bank_line_id, installment_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    code         TEXT NOT NULL,
    status       TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suspense_items (
    suspense_id   TEXT PRIMARY KEY,
    bank_line_id  TEXT NOT NULL,
    received_date TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posting_packs (
    pack_id    TEXT PRIMARY KEY,
    value_date TEXT NOT NULL,
    status     TEXT NOT NULL,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
