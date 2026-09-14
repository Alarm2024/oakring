-- oakring tick store.
-- `ts_utc` stays human-readable; `ts_epoch` is what the analyzer buckets on.
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL DEFAULT 0,
    pair TEXT NOT NULL,
    source TEXT NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL,
    spread_bps REAL,
    bid_qty REAL,
    ask_qty REAL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_ticks_pair_ts ON ticks (pair, ts_utc);
CREATE INDEX IF NOT EXISTS idx_ticks_pair_epoch ON ticks (pair, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_ticks_epoch ON ticks (ts_epoch);
