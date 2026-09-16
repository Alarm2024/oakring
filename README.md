# oakring

Record the market, then look back at the recording to find the cycle.

Two stdlib-only Python tools that share one SQLite file:

- **`recorder.py`** — polls Binance `bookTicker` for a watchlist on a fixed interval and stores bid/ask/mid/spread/size per pair per tick. Runs forever as a service.
- **`analyze.py`** — reads those ticks back, resamples them into bars, and reports coverage, trend, swings, dominant periods and the current cycle phase.

Record for a while first. A cycle you cannot see three of is not a cycle you have measured.

## Requirements

- Python 3.9+ (stdlib only, no packages to install; developed and tested on 3.11)
- Outbound HTTPS to Binance (no listen sockets, no wallets, no API keys — `bookTicker` is a public endpoint)
- Optional outbound HTTPS to [Jupiter](https://station.jup.ag/docs/apis/swap-api) for an on-chain SOL/USDC reference (public quote endpoint; API key only if your deployment requires it)

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
| `JUPITER_ENABLED` | off | Set `1` to sample a Jupiter quote on each tick |
| `JUPITER_ATTACH_PAIRS` | `SOLUSDT,SOLUSDC` | CEX pairs that receive `onchain_ref` / `basis_bps` columns |
| `JUPITER_DEXES` | unset | Comma-separated DEX labels for per-pool samples (e.g. `Raydium,Orca,Meteora DLMM`) |
| `JUPITER_ONLY_DIRECT_ROUTES` | `1` | Single-hop routes when sampling each DEX |
| `JUPITER_QUOTE_URL` | `https://quote-api.jup.ag/v6/quote` | Public quote endpoint |
| `JUPITER_INPUT_MINT` / `JUPITER_OUTPUT_MINT` | wrapped SOL / USDC | USDC leg for `*USDC` pairs |
| `JUPITER_USDT_MINT` / `JUPITER_USDT_DECIMALS` | mainnet USDT / `6` | USDT leg for `*USDT` pairs |
| `JUPITER_AMOUNT_LAMPORTS` | `1000000000` | Quote size (1 SOL) |
| `JUPITER_SLIPPAGE_BPS` | `50` | Slippage passed to the quote route finder |
| `JUPITER_INPUT_DECIMALS` / `JUPITER_OUTPUT_DECIMALS` | `9` / `6` | Decode raw mint amounts into a price |
| `JUPITER_API_KEY` | unset | Optional; read from env only, never commit |

## Analysing the recording

`analyze.py` opens the database read-only, so it is safe to run while the recorder is writing.

```bash
python3 analyze.py --latest                              # price right now, every pair
python3 analyze.py --list-pairs                          # what has been recorded
python3 analyze.py --auto                                # let it pick the settings
python3 analyze.py --cross --since 12h                   # USDT vs USDC vs the peg
python3 analyze.py --basis --since 12h                   # CEX mid vs Jupiter on-chain ref
python3 analyze.py --venues --since 12h                  # the same pair across exchanges
python3 analyze.py --since 7d  --bucket 1h               # every pair, the default view
python3 analyze.py --pair SOLUSDT --since 30d --bucket 4h --swing 3
python3 analyze.py --since 24h --bucket 15m --format json > report.json
python3 analyze.py --since 7d  --bucket 1h --format csv  > bars.csv
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--latest` | off | Current price per pair with 1h/24h/7d change, then exit |
| `--cross` | off | USDT vs USDC books against the peg, over `--since`, then exit |
| `--basis` | off | CEX mid vs on-chain Jupiter reference, held/edge periods, then exit |
| `--held-bps` | `15` | \|basis\| at or below this for `--basis-min-ticks` counts as held |
| `--edge-bps` | `40` | \|basis\| at or above this for `--basis-min-ticks` counts as edge |
| `--basis-min-ticks` | `3` | Minimum consecutive ticks for a held or edge period |
| `--venues` | off | Compare each pair across the venues recording it, then exit |
| `--venue` | all | Restrict any command to one venue |
| `--stale-after` | `5m` | In `--latest`, flag a pair whose last tick is older than this |
| `--since` | `7d` | Window back from now (`90m`, `24h`, `7d`, `2w`) |
| `--bucket` | `1h` | Resample size; the bar is the unit every cycle length is quoted in |
| `--swing` | `2.0` | Percent reversal that confirms a zigzag pivot |
| `--auto` | off | Pick `--bucket` and `--swing` per pair from the data present |
| `--pair` | all recorded | Repeatable |
| `--format` | `text` | `text`, `brief` (phone sized), `json` (full report), `csv` (the OHLC bars) |
| `--db` | `DB_PATH` | Override the database |

### Current prices

Cycle analysis needs history; `--latest` needs none, so it works the moment a pair starts recording:

```
$ python3 analyze.py --latest
pair                price     spread      age        1h       24h        7d
BTCUSDT         78,482.92    0.02bps      45s    -0.02%         -         -
ETHUSDT          2,533.45    0.02bps      45s    -0.02%         -         -
SOLUSDT          100.4032    1.00bps      45s    +0.66%    +0.03%    +0.20%
```

A `-` means that pair has not been recording long enough for that horizon yet — a pair added this morning has a 1h figure but no 24h one. It never means the pair stopped recording, and the table says so beneath itself whenever a `-` appears. **`age` is the column that tells you something is wrong**: it is how long ago the last priced tick landed, and anything over `--stale-after` is marked.

To confirm everything is recording, compare the hourly tick count in `./check.sh` against pairs x 60 — `0 of 420` across 7 pairs means every pair was polled every minute of the last hour with no failures.

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

### Letting it pick: `--auto`

The settings that matter depend on how much has been recorded and how much the pair moves, and getting them wrong produces a report that says nothing — 4-hour buckets over two days give 13 bars, and a 5% swing threshold never triggers on a pair whose whole range is 2.9%.

`--auto` derives both per pair: a bucket that turns the recording into roughly 150 bars, and a swing threshold scaled to that pair's own per-bar movement. Each pair gets its own, which is the point — BTC and SOL do not swing by the same percentage.

```bash
python3 analyze.py --since 30d --auto
```

The header line reports what it chose, so you can pin those values by hand afterwards:

```
window 2026-08-15T23:13:10Z -> 2026-09-14T23:13:10Z  bucket 15.0m  swing 1.14%
```

Without `--auto`, a report with too few bars now names the bucket that would have fitted instead of just complaining.

### Stablecoins and the peg

`USDCUSDT` and friends sit at roughly 1.0, so percentage change and cycle phase say little. What matters is the deviation from the peg and the spread, both of which the recorder already stores. Prices between 1 and 10 print to six decimals so a deviation of a basis point or two stays visible rather than rounding to a flat `1.0000`.

```sql
-- peg deviation in basis points, worst first
SELECT ts_utc, mid, ROUND((mid - 1.0) * 10000, 2) AS deviation_bps, spread_bps
FROM ticks
WHERE pair = 'USDCUSDT' AND note IS NULL
ORDER BY ABS(mid - 1.0) DESC
LIMIT 20;
```

USDC-quoted books (`SOLUSDC`, `BTCUSDC`, `ETHUSDC`) are ordinary pairs and analyse normally. Recording both `SOLUSDT` and `SOLUSDC` lets you compare the same asset across quote currencies.

### Picking a window and bucket

The bucket sets the shortest cycle you can see (about 4 bars) and the window sets the longest (about half the window). To measure a cycle of length *L*, record at least `3 × L` and pick a bucket near `L / 20`:

| Looking for | `--since` | `--bucket` | `--swing` |
|-------------|-----------|-----------|-----------|
| Intraday chop | `24h` | `5m` | `0.3` |
| Daily swing | `7d` | `1h` | `2` |
| Multi-week | `60d` | `4h` | `5` |

## Two commands from a phone

No laptop needed — both are one line over SSH.

**Is it working?**

```bash
./check.sh
```

Services, database size, free disk, tick count, how long ago the last tick landed, the error rate over the last hour, any pair that has stalled, and the current prices. Ends in `OK - recording.` or `NEEDS ATTENTION`, and exits non-zero in the second case so cron can use it too.

**The report, phone sized:**

```bash
python3 analyze.py --since 30d --auto --format brief
```

`brief` keeps every line under 40 characters so nothing wraps on a phone, and `--auto` picks the settings, so this one command works whether you have two days recorded or two months:

```
oakring  30.0d to 2026-09-14T23:22:36Z

SOLUSDT  107.5547
  move   +5.64% over 47.2h
  range  14.47%, now 39% up it
  cycle  ~9.2h (8 seen, 1.97% swings)
  repeat ~9.1h [low]
  phase  ACCUMULATION
  data   47.2h, 0 gaps, 0.0% errors
```

`move` is measured over the data that exists, which the `data` line states outright along with gaps and errors — so a report built on a broken recording says so.

## More than one venue

The `source` column records which exchange a row came from. Binance is polled from `WATCHLIST`; every other venue gets its own list, and a venue with no list is not polled at all:

```bash
WATCHLIST=SOLUSDT,BTCUSDT,ETHUSDT,USDCUSDT,SOLUSDC
WATCHLIST_COINBASE=SOLUSDC
WATCHLIST_KRAKEN=SOLUSDC
WATCHLIST_OKX=SOLUSDC
```

Supported: `binance`, `coinbase`, `kraken`, `okx`, `bybit` — all public endpoints, no keys.

Exchanges do not agree on names, and sometimes not on which pairs exist. `PAIR:VENUE_PAIR` records under the first name and asks the venue for the second:

```bash
WATCHLIST_COINBASE=SOLUSDC:SOLUSD
```

Coinbase lists no `SOL-USDC` at all — USD and USDC are interchangeable there, so `SOL-USD` *is* that book. Recording it as `SOLUSDC` puts it beside the other venues' SOL/USDC instead of stranding it under a name nothing else shares.

Check a venue actually carries a pair before adding it:

```bash
python3 recorder.py --probe SOLUSDC SOLUSDC:SOLUSD
```

```
venue      pair       result
binance    SOLUSDC    ok   bid=99.81  ask=99.82  mid=99.815  spread=1.00bps
coinbase   SOL-USDC   FAILED  error:HTTPError:404: Not Found
coinbase   SOL-USD    ok   bid=99.80  ask=99.83  mid=99.815  spread=3.01bps
kraken     SOLUSDC    ok   bid=99.79  ask=99.84  mid=99.815  spread=5.01bps
okx        SOL-USDC   ok   bid=99.81  ask=99.83  mid=99.82   spread=2.00bps
```

The probe prints each venue's own spelling, so a 404 tells you the name is wrong rather than the venue being down.

Only add the venues that say `ok`. The probe writes nothing.

### Comparing them

```bash
python3 analyze.py --venues --since 12h
```

```
SOLUSDC  2026-09-15T17:39:00Z  (binance, coinbase, kraken, okx)
  binance   bid 100.1728  ask 100.1826  mid 100.1777
  coinbase  bid 100.1669  ask 100.1909  mid 100.1789
  kraken    bid 100.1517  ask 100.1927  mid 100.1722
  okx       bid 100.1678  ask 100.1808  mid 100.1743
  gap      +0.67 bps (kraken cheapest, coinbase dearest)
  crossed  -0.80 bps now (best bid binance, best ask okx)
  over 6.0h: mean gap 0.75, widest 1.48 bps at 2026-09-15T17:29:00Z
  books crossed in 0.0% of 361 simultaneous ticks, at most +0.00 bps
```

`gap` is how far apart the mids are. `crossed` is the best bid anywhere minus the best ask anywhere — positive means one venue's bid sits above another's ask.

Only ticks where every venue priced are compared, since a fresh book against a missing one would invent a gap that was never there. All venues in a round share one timestamp, so these are simultaneous quotes.

**A crossed book is not free money.** Taker fees, withdrawal cost and transfer time all sit between the two sides and none are counted. This measures disagreement between venues, nothing more — the tool prints that caveat itself.

### Everything else stays venue-aware

Once a pair is on more than one venue, `--latest` and the cycle reports label it `SOLUSDC@kraken` and analyse each venue separately — two venues' books folding into one series would be meaningless. `--venue kraken` restricts any command to one venue and drops the suffix.

## The USDT / USDC cross

When the same asset is recorded against both quote currencies and `USDCUSDT` is recorded too, the three legs form a triangle:

```bash
python3 analyze.py --cross --since 12h
```

```
BTC  2026-09-14T23:31:00Z
  books    USDT 78,425.85 / USDC 78,421.80
  implied  +0.52 bps   peg says +0.60 bps
  residual -0.08 bps vs 0.05 bps of spread  -> outside cost
  over 6.0h: mean +0.00, sd 0.22, widest -0.66 bps
  beyond spread cost in 81.99% of 361 aligned ticks
```

`implied` is what the two books say USDC is worth (`mid_USDT / mid_USDC`); `peg says` is what `USDCUSDT` trades at; `residual` is the disagreement. The recorder stamps every pair in a tick with one timestamp, so all three legs come from the same instant — no interpolation and no stale leg.

`cost` is half a spread on each of the three legs. **Exchange fees are not included** and are usually several bps, far wider than these residuals — so `outside cost` means the disagreement is measurable, not that it is profitable. The tool prints that caveat itself.

The percentage is the useful number over time: a pair whose residual sits inside the spread all day is quoted coherently, while one that spends most of its time outside a very tight spread is mostly showing you measurement noise.

## CEX vs on-chain basis (Jupiter)

Optional dry measurement: when `JUPITER_ENABLED=1`, the recorder fetches Jupiter public quotes on each tick and stores them next to the CEX book on the configured pairs (`JUPITER_ATTACH_PAIRS`, default `SOLUSDT,SOLUSDC`). Both legs share the same timestamp, so basis is available at tick resolution — e.g. ~1s if `INTERVAL_SEC=1`, instead of the ~30s journal samples elsewhere.

Two layers are recorded:

1. **Aggregated** — one best-route quote on the `ticks` row (`onchain_ref`, `basis_bps`), same as before.
2. **Per-pool** — when `JUPITER_DEXES` is set, parallel quotes restricted to each DEX (Raydium, Orca, Meteora DLMM, …) land in the `pool_samples` child table. SOLUSDC pairs quote against USDC; SOLUSDT pairs quote against USDT automatically.

```bash
# in ~/.config/oakring/.env
JUPITER_ENABLED=1
JUPITER_DEXES=Raydium,Orca,Meteora DLMM
JUPITER_ATTACH_PAIRS=SOLUSDT,SOLUSDC
INTERVAL_SEC=1

# then, after recording:
python3 analyze.py --basis --since 12h
python3 analyze.py --basis --since 2h --held-bps 15 --edge-bps 40 --format json
```

`basis_bps = (cex_mid - pool_ref) / pool_ref * 10000` per leg. `--basis` reports aggregated and **per-pool** held/edge periods, plus **cross-pool spread** (cheapest pool vs dearest pool on the same tick — same-pool vs cross-pool visibility). No swap is built, no wallet is touched, and no API key is required for the public quote endpoint unless your deployment needs one via `JUPITER_API_KEY`.

**This is eyes, not send.** Route impact, latency, fees and execution path are not counted — the same caveat as `--cross`.

## Alerts

A recorder that dies quietly is the one real risk to a project like this: the history you wanted is simply missing, and you find out days later. `alert.py` watches for that and tells you.

Configure a destination in `~/.config/oakring/.env` — Telegram, Discord, or any endpoint that accepts a JSON POST:

```bash
ALERT_TELEGRAM_TOKEN=123456:ABCdef...
ALERT_TELEGRAM_CHAT_ID=987654321
# or
ALERT_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
# or anything that takes {"text": "..."}
ALERT_WEBHOOK=https://example.test/hook
```

Prove it works, then install the timer:

```bash
python3 alert.py --test
sudo cp /home/ubuntu/oakring/oakring-alert.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now oakring-alert.timer
```

It checks every five minutes but **only sends on a change of state** — broke, recovered, or a new problem appearing — so the channel stays worth reading. While a problem persists it repeats every `ALERT_REPEAT_HOURS` (default 6) rather than every five minutes. A healthy first run says nothing at all: announcing good health on install just teaches you to ignore it.

What counts as a problem:

| Check | Fires when |
|-------|-----------|
| Recorder stopped | No tick from any pair for longer than `--stale-after` (default 5m) |
| One pair quiet | A pair stops while the others keep going — a delisted or renamed symbol |
| Sustained failures | Over 50% of the last hour's ticks errored |
| Disk filling | Under 512 MB free where the database lives |
| Database gone | The file is missing, or holds no ticks |

`python3 alert.py --dry-run` prints what it would send without sending or advancing its state. The same checks back `./check.sh` and `python3 health.py`, so what you see by hand is exactly what triggers an alert.

A note on the bot token: it lives in `.env` at mode `600`, and a Telegram token travels inside the request URL. `alert.py` never logs a URL for that reason — a failed send reports the transport name, the HTTP status, and the API's own explanation from the response body, with any configured secret masked if a misconfigured endpoint echoes one back. There are tests asserting both halves.

If Telegram returns **HTTP 400**, the body says why. Usually it is `chat not found`, meaning the chat id is wrong or you have not sent the bot a message yet — a bot cannot open a conversation with you. Message the bot once, then read your id:

```bash
TOKEN=$(sed -n 's/^ALERT_TELEGRAM_TOKEN=//p' ~/.config/oakring/.env)
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | head -c 600
```

The id is `result[].message.chat.id` — negative for a group. Put that number in `.env`; a chat id that is not a number (or `@channelname`) is reported as a configuration problem before anything is sent, rather than coming back as `chat not found`.

Transports are independent, so a broken Telegram never stops Discord from delivering.

## Scheduled reports

`report.sh` writes a timestamped report into `~/.config/oakring/reports/` and prints the path. Any argument is passed through to `analyze.py`:

```bash
./report.sh                                   # the default week view
./report.sh --since 30d --bucket 4h --swing 5 # the month view
./report.sh --since 30d --format json         # machine-readable
```

A failed run leaves no half-written file behind.

To have it run itself, install the timer (once the recorder is running and approved):

```bash
sudo cp /home/ubuntu/oakring/oakring-report.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oakring-report.timer
systemctl list-timers oakring-report.timer     # when it next fires
```

Weekly on Monday 00:05 UTC. For a monthly report instead, change `OnCalendar` in the timer to `*-*-01 00:05:00`. `Persistent=true` means a report missed while the droplet was down runs at the next boot. Run one immediately with `sudo systemctl start oakring-report.service`, and read the last outcome with `journalctl -u oakring-report`.

## Downloading the recording

The database runs in WAL mode, so copying `ring.db` with `scp` while the recorder is writing can capture a torn file. Take a consistent snapshot first — this is safe on a live database:

```bash
sqlite3 ~/.config/oakring/ring.db ".backup /tmp/ring-snapshot.db"
```

Then, from the machine you want it on:

```bash
scp ubuntu@DROPLET:/tmp/ring-snapshot.db .
python3 analyze.py --db ring-snapshot.db --since 30d --bucket 4h
```

The analyzer only needs the file, so the same commands work anywhere with Python 3.9+. To pull the generated reports instead of the raw data:

```bash
scp -r ubuntu@DROPLET:/home/ubuntu/.config/oakring/reports .
```

A month of three pairs at 60s is roughly 130k rows — a few tens of MB, and it compresses well with `gzip` if the link is slow.

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
| `onchain_ref` | Jupiter-implied price for the configured mint pair (e.g. USDC per SOL), sampled on the same tick |
| `onchain_impact_bps` | Route price impact from the Jupiter quote, in basis points |
| `basis_bps` | `(mid - onchain_ref) / onchain_ref * 10000` when both legs are present |
| `onchain_note` | NULL when the Jupiter quote succeeded, otherwise `error:HTTPError:…`, `error:URLError`, … |

`pool_samples`, one row per DEX per attached pair per tick (when `JUPITER_DEXES` is set):

| Column | Notes |
|--------|-------|
| `ts_utc` / `ts_epoch` | Same instant as the parent CEX tick |
| `pair` | CEX pair, e.g. `SOLUSDC` |
| `dex` | Jupiter DEX label, e.g. `Raydium` |
| `ref_price` | Implied on-chain mid from that pool's quote |
| `basis_bps` | CEX mid vs this pool at the same timestamp |
| `impact_bps`, `in_amount`, `out_amount`, `amm_key` | From the Jupiter quote / routePlan |
| `note` | NULL when good, otherwise an error marker |

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
| `recorder.py` | Poll loop: every venue, retries, error rows, pruning, clean shutdown |
| `jupiter.py` | Optional Jupiter public quote fetch and on-chain reference price |
| `venues.py` | Per-exchange symbols, URLs and response parsing |
| `report.sh` | Writes a timestamped report; what the timer runs |
| `check.sh` | Health check: services, freshness, errors, prices |
| `health.py` | The checks themselves, shared by check.sh and alert.py |
| `alert.py` | Notifies Telegram/Discord/webhook when recording breaks |
| `analyze.py` | Cycle report: bars, coverage, trend, swings, periodogram, phase |
| `common.py` | Shared config, database open/migrate, time helpers |
| `schema.sql` | `ticks` table and indexes |
| `tests/test_oakring.py` | Offline test suite |
| `tests/test_jupiter.py` | Jupiter quote parsing, recorder attach, `--basis` analysis |
| `tests/fixtures/jupiter_quote_*.json` | Sample Jupiter `/v6/quote` responses (aggregated + per-DEX) |
| `.env.example` | Sample configuration (copy to `~/.config/oakring/.env`) |
| `oakring.service` | Hardened systemd unit template |
| `oakring-report.{service,timer}` | Scheduled weekly report |
| `oakring-alert.{service,timer}` | Health check every five minutes |
