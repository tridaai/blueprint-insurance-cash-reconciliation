-- Multi-currency cash application — SQLite dialect. Same change as
-- migrations/0005_currency.sql (PostgreSQL): the denomination as a
-- key column beside the payload. Existing rows are USD — the only
-- currency on the books before this migration.
ALTER TABLE bank_lines      ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE receivables     ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE allocations     ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE suspense_items  ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE payment_returns ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
