-- Core records for premium cash reconciliation.
-- Entity payloads are JSONB (amounts as decimal strings, dates ISO);
-- the columns beside them exist for the queries the desk actually runs.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS brokers (
    broker_id TEXT PRIMARY KEY,
    payload   JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS receivables (
    policy_number TEXT PRIMARY KEY,
    broker_id     TEXT,
    status        TEXT NOT NULL,
    payload       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_lines (
    bank_line_id TEXT PRIMARY KEY,
    value_date   DATE NOT NULL,
    amount       NUMERIC(14, 2) NOT NULL,
    payload      JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS remittances (
    remittance_id TEXT PRIMARY KEY,
    received_date DATE NOT NULL,
    text_hash     TEXT NOT NULL,
    payload       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    bank_line_id   TEXT NOT NULL,
    policy_number  TEXT NOT NULL,
    installment_id TEXT NOT NULL,
    amount         NUMERIC(14, 2) NOT NULL,
    tier           TEXT,
    pack_id        TEXT,
    payload        JSONB NOT NULL,
    PRIMARY KEY (bank_line_id, installment_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    code         TEXT NOT NULL,
    status       TEXT NOT NULL,
    first_seen   DATE NOT NULL,
    payload      JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS suspense_items (
    suspense_id   TEXT PRIMARY KEY,
    bank_line_id  TEXT NOT NULL,
    received_date DATE NOT NULL,
    payload       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS posting_packs (
    pack_id    TEXT PRIMARY KEY,
    value_date DATE NOT NULL,
    status     TEXT NOT NULL,
    payload    JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id      BIGSERIAL PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
