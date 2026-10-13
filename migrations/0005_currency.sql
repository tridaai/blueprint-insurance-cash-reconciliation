-- Multi-currency cash application: the denomination becomes a key
-- column, as it already is in every payload. Cash, receivables,
-- allocations, suspense, and returns each carry the ISO currency
-- their amounts are denominated in, so the books can be queried
-- per currency without opening a payload. Existing rows are USD —
-- the only currency on the books before this migration.
ALTER TABLE bank_lines      ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE receivables     ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE allocations     ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE suspense_items  ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE payment_returns ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
