CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    pair TEXT NOT NULL,
    source TEXT NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL,
    spread_bps REAL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_ticks_pair_ts ON ticks (pair, ts_utc);
