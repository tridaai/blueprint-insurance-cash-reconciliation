-- Payment returns — SQLite dialect. Same table and semantics as
-- migrations/0003_payment_returns.sql (PostgreSQL); money is TEXT
-- holding a decimal string, as everywhere in this dialect.
CREATE TABLE IF NOT EXISTS payment_returns (
    return_id    TEXT PRIMARY KEY,
    bank_line_id TEXT NOT NULL,
    return_date  TEXT NOT NULL,
    amount       TEXT NOT NULL,
    payload      TEXT NOT NULL
);
