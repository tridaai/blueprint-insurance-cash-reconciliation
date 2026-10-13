-- Day-run claims — SQLite dialect. Same table, columns and
-- semantics as migrations/0006_day_claims.sql (PostgreSQL); the
-- day is stored as its ISO text, as with every other date key in
-- this dialect, and the payload is JSON text.
CREATE TABLE IF NOT EXISTS day_claims (
    day       TEXT PRIMARY KEY,
    attempt   INTEGER NOT NULL,
    actor     TEXT NOT NULL,
    state     TEXT NOT NULL,
    payload   TEXT NOT NULL
);
