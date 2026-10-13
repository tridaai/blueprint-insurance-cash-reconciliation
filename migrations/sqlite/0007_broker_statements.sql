-- Broker statements — SQLite dialect. Same table, columns and
-- semantics as migrations/0007_broker_statements.sql (PostgreSQL);
-- the payload is JSON text.
CREATE TABLE IF NOT EXISTS broker_statements (
    statement_id TEXT PRIMARY KEY,
    broker_id    TEXT NOT NULL,
    period       TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS broker_statements_broker_period
    ON broker_statements (broker_id, period);
