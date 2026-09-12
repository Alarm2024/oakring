# oakring

Binance `bookTicker` SQLite recorder. Polls configured pairs on an interval and stores bid/ask/mid/spread in a local database.

## Requirements

- Python 3 (stdlib only)
- Outbound HTTPS to Binance (no listen sockets, no wallets)

## Install

Clone or copy this repo to `/home/ubuntu/oakring`.

### Config (for real)

```bash
mkdir -p /home/ubuntu/.config/oakring && chmod 700 /home/ubuntu/.config/oakring
cp .env.example /home/ubuntu/.config/oakring/.env && chmod 600 /home/ubuntu/.config/oakring/.env
# code at /home/ubuntu/oakring
```

Edit `/home/ubuntu/.config/oakring/.env` if you need a different watchlist, interval, or DB path.

### Run manually

```bash
/usr/bin/python3 /home/ubuntu/oakring/recorder.py
```

One line is printed per pair per tick. The recorder creates the schema on first run and enforces mode `700` on the config directory and `600` on the database file.

### systemd unit (install only — do not start yet)

Copy the unit file for the owner to review:

```bash
sudo cp /home/ubuntu/oakring/oakring.service /etc/systemd/system/oakring.service
sudo systemctl daemon-reload
```

**Do NOT `systemctl start` or `systemctl enable` until the owner says so.**

This service does not open any extra ports.

## Safety

- Do **not** touch 350-bot keys or credentials.
- Do **not** change `ufw` rules as part of this project.

## SQLite queries

Open the database (default path from `.env.example`):

```bash
sqlite3 /home/ubuntu/.config/oakring/ring.db
```

Recent ticks for all pairs:

```sql
SELECT ts_utc, pair, bid, ask, mid, spread_bps, note
FROM ticks
ORDER BY id DESC
LIMIT 20;
```

Latest tick per pair:

```sql
SELECT t.*
FROM ticks t
JOIN (
  SELECT pair, MAX(id) AS max_id
  FROM ticks
  GROUP BY pair
) latest ON t.id = latest.max_id;
```

Error rows only:

```sql
SELECT ts_utc, pair, note
FROM ticks
WHERE note IS NOT NULL
ORDER BY id DESC;
```

Spread history for one pair:

```sql
SELECT ts_utc, mid, spread_bps
FROM ticks
WHERE pair = 'SOLUSDT' AND note IS NULL
ORDER BY ts_utc DESC
LIMIT 100;
```

## Files

| File | Purpose |
|------|---------|
| `recorder.py` | Main loop: fetch Binance bookTicker, insert ticks |
| `schema.sql` | `ticks` table and indexes |
| `.env.example` | Sample configuration (copy to `~/.config/oakring/.env`) |
| `oakring.service` | systemd unit template |
