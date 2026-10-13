-- Broker statements: a broker's own monthly statement of what it
-- says it remitted, imported as a record of its own so the broker
-- statement reconciliation can cite exactly which statement it
-- read. One standing statement per (broker, period): a corrected
-- statement supersedes the standing one on its own record (the
-- superseded_by field inside the payload), and the old statement
-- stays on the books as history. The statement's figures are
-- claims, never postings — importing one changes no money.
CREATE TABLE IF NOT EXISTS broker_statements (
    statement_id TEXT PRIMARY KEY,
    broker_id    TEXT NOT NULL,
    period       TEXT NOT NULL,
    payload      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS broker_statements_broker_period
    ON broker_statements (broker_id, period);
