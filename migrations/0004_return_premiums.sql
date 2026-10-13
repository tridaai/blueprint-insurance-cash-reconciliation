-- Return premiums: cancellations, the payables they open, and the
-- payout packs that settle them at the gate. Payloads mirror the
-- other entity tables; the payables ledger is its own ledger and
-- never enters the bank identity.
CREATE TABLE IF NOT EXISTS cancellations (
    cancellation_id TEXT PRIMARY KEY,
    policy_number   TEXT NOT NULL,
    effective_date  DATE NOT NULL,
    recorded_on     DATE NOT NULL,
    payload         JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS return_premium_payables (
    payable_id    TEXT PRIMARY KEY,
    policy_number TEXT NOT NULL,
    status        TEXT NOT NULL,
    opened_on     DATE NOT NULL,
    payload       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS payout_packs (
    pack_id    TEXT PRIMARY KEY,
    value_date DATE NOT NULL,
    status     TEXT NOT NULL,
    payload    JSONB NOT NULL
);
