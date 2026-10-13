-- Day-run claims: the store-level record that makes "a day runs
-- once" true across processes. One row per day, keyed by the day
-- itself: the first starter's insert wins atomically, a second
-- starter — even mid-run of the first — meets the unique key and
-- is refused. A completed run's row is the day's standing claim;
-- an interrupted attempt's row is reclaimed, by compare-and-swap
-- on (day, attempt, state), only by the sanctioned recovery run.
CREATE TABLE IF NOT EXISTS day_claims (
    day       DATE PRIMARY KEY,
    attempt   INTEGER NOT NULL,
    actor     TEXT NOT NULL,
    state     TEXT NOT NULL,
    payload   JSONB NOT NULL
);
