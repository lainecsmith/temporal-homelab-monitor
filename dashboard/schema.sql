-- Read-side copy of what Temporal already knows, owned by the dashboard API service.
-- Temporal stays the source of truth for live state. Nothing here is written by workers.
-- Timestamps are ISO-8601 UTC text, the same strings the workflow puts in the snapshot.

PRAGMA journal_mode = WAL;     -- one writer (the poller), many readers (page loads)
PRAGMA foreign_keys = ON;

-- 1. Last-known snapshot per host. Lets the UI show data while the controller or
--    Temporal is down, and across API restarts.
CREATE TABLE IF NOT EXISTS host_snapshot (
    host                TEXT PRIMARY KEY,
    fetched_at          TEXT NOT NULL,   -- when the API service last got a snapshot from Temporal
    status              TEXT NOT NULL,   -- online | offline | unknown, as the workflow reported it
    last_seen           TEXT,            -- the workflow's last_seen (last successful collection)
    updates_checked_at  TEXT,            -- ingest skips update tracking when this hasn't changed
    -- Denormalized so the fleet view doesn't parse JSON for every host
    os_updates          INTEGER,
    security_updates    INTEGER,
    image_updates       INTEGER,
    reboot_required     INTEGER,         -- 0 / 1 / NULL when unknown
    snapshot_json       TEXT NOT NULL
);

-- 2. Update aging: how long has each pending update been waiting?
--    One OPEN row (resolved_at IS NULL) per item. When an item disappears from a
--    completed check it is resolved. If it returns later, that starts a new row and a
--    new clock, so history is kept and "available for N days" stays honest.
CREATE TABLE IF NOT EXISTS pending_update (
    id                 INTEGER PRIMARY KEY,
    host               TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('os_package', 'image_build', 'image_tag')),
    name               TEXT NOT NULL,    -- package name, or the image reference as the container uses it
    first_seen_at      TEXT NOT NULL,    -- when a check first reported it
    last_seen_at       TEXT NOT NULL,    -- most recent check that still reported it
    resolved_at        TEXT,             -- first check that no longer reported it (NULL = still pending)
    current_version    TEXT,
    available_version  TEXT,             -- updated as newer versions appear; first_seen_at is not reset
    is_security        INTEGER,          -- 0 / 1 / NULL when the package manager doesn't say
    detail_json        TEXT              -- e.g. image digests, newest_tag vs newest_tag_same_major
);

CREATE UNIQUE INDEX IF NOT EXISTS pending_update_one_open
    ON pending_update (host, kind, name) WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS pending_update_by_host
    ON pending_update (host, resolved_at);

-- Open updates with their age, oldest first. Security updates sort ahead of the rest at equal age.
CREATE VIEW IF NOT EXISTS open_update_age AS
SELECT host, kind, name, current_version, available_version, is_security,
       first_seen_at,
       CAST(julianday('now') - julianday(first_seen_at) AS INTEGER) AS age_days
FROM pending_update
WHERE resolved_at IS NULL
ORDER BY age_days DESC, is_security DESC;

-- 3. Phase 2 (trends and sparklines). Safe to skip until you want graphs.
--    Written by the poller each cycle; prune with:
--    DELETE FROM metric_sample WHERE ts < datetime('now', '-14 days');
CREATE TABLE IF NOT EXISTS metric_sample (
    host           TEXT NOT NULL,
    ts             TEXT NOT NULL,
    cpu_pct        REAL,
    mem_used_mb    INTEGER,
    mem_total_mb   INTEGER,
    disk_used_pct  REAL,
    load1          REAL,
    PRIMARY KEY (host, ts)
) WITHOUT ROWID;
