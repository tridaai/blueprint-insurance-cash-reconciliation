-- Payment returns: bank return advices worked against applied lines.
-- The return is its own record; the original application is never
-- edited away. Payload mirrors the other entity tables.
CREATE TABLE IF NOT EXISTS payment_returns (
    return_id    TEXT PRIMARY KEY,
    bank_line_id TEXT NOT NULL,
    return_date  DATE NOT NULL,
    amount       NUMERIC(14, 2) NOT NULL,
    payload      JSONB NOT NULL
);
