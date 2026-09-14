# oakring

Record the market, then look back at the recording to find the cycle.

Two stdlib-only Python tools that share one SQLite file:

- **`recorder.py`** — polls Binance `bookTicker` for a watchlist on a fixed interval and stores bid/ask/mid/spread/size per pair per tick. Runs forever as a service.
- **`analyze.py`** — reads those ticks back, resamples them into bars, and reports coverage, trend, swings, dominant periods and the current cycle phase.

Record for a while first. A cycle you cannot see three of is not a cycle you have measured.

## Requirements

- Python 3.11+ (stdlib only, no packages to install)
- Outbound HTTPS to Binance (no listen sockets, no wallets, no API keys — `bookTicker` is a public endpoint)

## Install

Clone or copy this repo to `/home/ubuntu/oakring`.

```bash
mkdir -p /home/ubuntu/.config/oakring && chmod 700 /home/ubuntu/.config/oakring
cp .env.example /home/ubuntu/.config/oakring/.env && chmod 600 /home/ubuntu/.config/oakring/.env
```

Edit `/home/ubuntu/.config/oakring/.env` for the watchlist, interval, DB path and retention. Every key is also readable from the real process environment, which takes precedence — handy for one-off runs.

### Run manually

```bash
/usr/bin/python3 /home/ubuntu/oakring/recorder.py
```

It logs one line per pair per tick, creates the schema on first run, migrates an older database in place, and enforces mode `700` on the config directory and `600` on the database. `Ctrl-C` (or `SIGTERM`) finishes the tick in flight and exits cleanly.

To try either tool without touching `/home/ubuntu`, point `OAKRING_CONFIG_DIR` somewhere else:

```bash
OAKRING_CONFIG_DIR=/tmp/oakring-test python3 recorder.py
```

### systemd unit (install only — do not start yet)

```bash
sudo cp /home/ubuntu/oakring/oakring.service /etc/systemd/system/oakring.service
sudo systemd-analyze verify /etc/systemd/system/oakring.service
sudo systemctl daemon-reload
```

**Do NOT `systemctl start` or `systemctl enable` until the owner says so.**

This service opens no ports. It runs with no privileges, a read-only view of the filesystem, and `/home/ubuntu/.config/oakring` as the only writable path. If it ever fails to start on an older systemd, the sandboxing block at the bottom of the unit is the first thing to trim.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `WATCHLIST` | `SOLUSDT` | Comma-separated pairs, fetched in one batched request per tick |
| `INTERVAL_SEC` | `60` | Seconds between ticks, aligned to the wall clock |
| `DB_PATH` | `~/.config/oakring/ring.db` | SQLite file |
| `BINANCE_URL` | `.../api/v3/ticker/bookTicker` | Endpoint |
| `HTTP_TIMEOUT_SEC` | `15` | Per-request timeout |
| `MAX_RETRIES` | `2` | Retries per tick, exponential backoff, then a per-pair fallback |
| `RETENTION_DAYS` | `0` (keep all) | Prune ticks older than this, checked hourly |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Analysing the recording

`analyze.py` opens the database read-only, so it is safe to run while the recorder is writing.

```bash
python3 analyze.py --list-pairs                          # what has been recorded
python3 analyze.py --since 7d  --bucket 1h               # every pair, the default view
python3 analyze.py --pair SOLUSDT --since 30d --bucket 4h --swing 3
python3 analyze.py --since 24h --bucket 15m --format json > report.json
python3 analyze.py --since 7d  --bucket 1h --format csv  > bars.csv
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--since` | `7d` | Window back from now (`90m`, `24h`, `7d`, `2w`) |
| `--bucket` | `1h` | Resample size; the bar is the unit every cycle length is quoted in |
| `--swing` | `2.0` | Percent reversal that confirms a zigzag pivot |
| `--pair` | all recorded | Repeatable |
| `--format` | `text` | `text`, `json` (full report), `csv` (the OHLC bars) |
| `--db` | `DB_PATH` | Override the database |

### Reading the report

```
=== SOLUSDT ===
window 2026-08-30T19:27:54Z -> 2026-09-14T19:27:54Z  bucket 1.0h
coverage   336/360 bars (93.3% of the window, 100.0% of what was recorded), 336 ticks, 0 errored (0.0%), 0 gaps
price      100.0000 -> 106.0474 (+6.05%)  range 95.7200..111.0000 (15.96%)
           high 2026-09-13T07:00:00Z, low 2026-09-02T07:00:00Z, now -4.46% from high / +10.79% from low
           spread avg 2.00 bps (max 2.00), book imbalance +0.500
trend      +0.37%/day over the window (R2 0.16), recent -0.47%/day, daily vol 2.19%
swings     14 pivots at 2.0% (7 peaks / 7 troughs), 12 completed cycles
           cycle length mean 2.0d / median 2.0d, mean leg 9.67%
           current leg up for 11.0h (+4.50% since the last trough)
periodicity
             ~2.0d   (48.08 bars, 6.99 cycles in window) power 10.6% [moderate]
phase      DISTRIBUTION - 68% up the window range, recent -0.47%/day vs +/-1.10%/day trend threshold
```

- **coverage** — check this first. Two percentages: how much of the requested window exists, and how complete the part you did record is. A low second number means the recorder was down or erroring, and every statistic below is reading a broken recording.
- **price** — endpoints, range, and where the last price sits relative to the window's high and low. `spread` and `book imbalance` (`+1` all bid size, `-1` all ask size) come from the top of book.
- **trend** — a least-squares slope on log price as % per day, with R² as how straight that line actually is. A big slope with a small R² is noise, not a trend.
- **swings** — zigzag pivots: a peak or trough is only confirmed once price reverses by `--swing`. Cycle length is measured peak-to-peak and trough-to-trough, so a "2.0d cycle" means roughly two days from one top to the next. A pivot on the first bar is discarded, since a window starting mid-swing did not witness that turn.
- **periodicity** — a periodogram over detrended log price. It reports the strongest repeating periods independently of the swing threshold, which is the useful cross-check: when the zigzag and the periodogram agree on a length, the cycle is probably real. `cycles_in_window` below 3 is marked low confidence — that is too little history to claim a period.
- **phase** — a Wyckoff-style four-phase read (accumulation, markup, distribution, markdown, or ranging) from where price sits in the range and whether the recent leg is moving faster than half a daily sigma. It is a heuristic summary of the recording, not a signal, and it says nothing about what happens next.

### Picking a window and bucket

The bucket sets the shortest cycle you can see (about 4 bars) and the window sets the longest (about half the window). To measure a cycle of length *L*, record at least `3 × L` and pick a bucket near `L / 20`:

| Looking for | `--since` | `--bucket` | `--swing` |
|-------------|-----------|-----------|-----------|
| Intraday chop | `24h` | `5m` | `0.3` |
| Daily swing | `7d` | `1h` | `2` |
| Multi-week | `60d` | `4h` | `5` |

## SQLite queries

```bash
sqlite3 /home/ubuntu/.config/oakring/ring.db
```

Recent ticks for all pairs:

```sql
SELECT ts_utc, pair, bid, ask, mid, spread_bps, note
FROM ticks ORDER BY id DESC LIMIT 20;
```

Latest tick per pair:

```sql
SELECT t.* FROM ticks t
JOIN (SELECT pair, MAX(id) AS max_id FROM ticks GROUP BY pair) latest
  ON t.id = latest.max_id;
```

Error rows only (these are the gaps in the recording):

```sql
SELECT ts_utc, pair, note FROM ticks WHERE note IS NOT NULL ORDER BY id DESC;
```

Hourly bars for one pair, straight from SQL:

```sql
SELECT datetime(ts_epoch - ts_epoch % 3600, 'unixepoch') AS hour,
       MIN(mid) AS low, MAX(mid) AS high, AVG(mid) AS avg_mid,
       AVG(spread_bps) AS avg_spread, COUNT(*) AS ticks
FROM ticks
WHERE pair = 'SOLUSDT' AND note IS NULL
GROUP BY hour ORDER BY hour DESC LIMIT 48;
```

## Schema

`ticks`, one row per pair per tick. Failed fetches are stored too, with `mid` NULL and `note` set, so a gap in the record is visible instead of invisible.

| Column | Notes |
|--------|-------|
| `ts_utc` / `ts_epoch` | The same instant, readable and numeric; the analyzer buckets on the epoch |
| `pair`, `source` | e.g. `SOLUSDT`, `binance` |
| `bid`, `ask`, `mid`, `spread_bps` | NULL on an errored tick |
| `bid_qty`, `ask_qty` | Top-of-book sizes, used for the imbalance figure |
| `note` | NULL when good, otherwise `error:HTTPError:418`, `error:URLError`, `error:CrossedBook`, … |

A database written by the first version of the recorder is migrated in place on the next start: the new columns are added and `ts_epoch` is backfilled from `ts_utc`.

## Tests

No network, no writes outside a temp directory:

```bash
python3 -m unittest discover -s tests -v
```

## Safety

- Do **not** touch 350-bot keys or credentials.
- Do **not** change `ufw` rules as part of this project.
- The recorder only reads a public endpoint. There is no trading logic here, and nothing in this repo places an order.

## Files

| File | Purpose |
|------|---------|
| `recorder.py` | Poll loop: batched fetch, retries, error rows, pruning, clean shutdown |
| `analyze.py` | Cycle report: bars, coverage, trend, swings, periodogram, phase |
| `common.py` | Shared config, database open/migrate, time helpers |
| `schema.sql` | `ticks` table and indexes |
| `tests/test_oakring.py` | Offline test suite |
| `.env.example` | Sample configuration (copy to `~/.config/oakring/.env`) |
| `oakring.service` | Hardened systemd unit template |
