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
    note TEXT,
    onchain_ref REAL,
    onchain_impact_bps REAL,
    basis_bps REAL,
    onchain_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_ticks_pair_ts ON ticks (pair, ts_utc);
CREATE INDEX IF NOT EXISTS idx_ticks_pair_epoch ON ticks (pair, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_ticks_epoch ON ticks (ts_epoch);

-- Per-DEX on-chain samples aligned to a CEX pair tick (same ts_epoch).
CREATE TABLE IF NOT EXISTS pool_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL,
    pair TEXT NOT NULL,
    dex TEXT NOT NULL,
    ref_price REAL,
    basis_bps REAL,
    impact_bps REAL,
    in_amount INTEGER,
    out_amount INTEGER,
    amm_key TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_pool_samples_pair_epoch ON pool_samples (pair, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_pool_samples_pair_dex_epoch ON pool_samples (pair, dex, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_pool_samples_epoch ON pool_samples (ts_epoch);
