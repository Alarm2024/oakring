#!/bin/sh
# Is oakring working? One short screen, sized for a phone.
#
#   ./check.sh
#
# Exit status is 0 when everything looks healthy, 1 when something needs
# attention, so it is also usable from a cron job or another script.
set -u

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DB="${OAKRING_DB:-${HOME}/.config/oakring/ring.db}"
PYTHON="${PYTHON:-/usr/bin/python3}"
STALE_SEC="${STALE_SEC:-300}"
PROBLEM=0

say() { printf '%-10s %s\n' "$1" "$2"; }

# --- services (absent systemd is fine: the recorder may be run by hand)
if command -v systemctl >/dev/null 2>&1; then
    RECORDER=$(systemctl is-active oakring 2>/dev/null || echo "not installed")
    say "recorder" "$RECORDER"
    [ "$RECORDER" = "active" ] || PROBLEM=1
    say "report" "$(systemctl is-active oakring-report.timer 2>/dev/null || echo 'not installed')"
fi

# --- database
if [ ! -f "$DB" ]; then
    say "database" "MISSING at $DB"
    exit 1
fi
say "database" "$(du -h "$DB" | cut -f1) at $DB"
say "disk" "$(df -h "$(dirname "$DB")" | awk 'NR==2 {print $4" free ("$5" used)"}')"

# --- freshness and errors, straight from the ticks
SUMMARY=$("$PYTHON" - "$DB" "$STALE_SEC" <<'PY'
import sqlite3, sys, time
db, stale_after = sys.argv[1], int(sys.argv[2])
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
epoch = "COALESCE(NULLIF(ts_epoch,0), CAST(strftime('%s', ts_utc) AS INTEGER))"
now = int(time.time())
pairs, total, newest = conn.execute(
    f"SELECT COUNT(DISTINCT pair), COUNT(*), MAX({epoch}) FROM ticks").fetchone()
if not total:
    print("EMPTY"); raise SystemExit
age = now - int(newest or 0)
errors, recent = conn.execute(
    f"SELECT SUM(note IS NOT NULL), COUNT(*) FROM ticks WHERE {epoch} > ?", (now - 3600,)).fetchone()
stale = [row[0] for row in conn.execute(
    f"SELECT pair, MAX({epoch}) FROM ticks GROUP BY pair HAVING ? - MAX({epoch}) > ?",
    (now, stale_after))]
print(f"{pairs}|{total}|{age}|{errors or 0}|{recent or 0}|{','.join(stale)}")
PY
)

if [ "$SUMMARY" = "EMPTY" ]; then
    say "ticks" "none recorded yet"
    exit 1
fi

PAIRS=$(echo "$SUMMARY" | cut -d'|' -f1)
TOTAL=$(echo "$SUMMARY" | cut -d'|' -f2)
AGE=$(echo "$SUMMARY" | cut -d'|' -f3)
ERRORS=$(echo "$SUMMARY" | cut -d'|' -f4)
RECENT=$(echo "$SUMMARY" | cut -d'|' -f5)
STALE=$(echo "$SUMMARY" | cut -d'|' -f6)

say "ticks" "$TOTAL across $PAIRS pairs"
if [ "$AGE" -gt "$STALE_SEC" ]; then
    say "last tick" "${AGE}s ago - STALE"
    PROBLEM=1
else
    say "last tick" "${AGE}s ago"
fi
say "errors 1h" "$ERRORS of $RECENT"
[ "$ERRORS" -gt 0 ] && [ "$ERRORS" -eq "$RECENT" ] && PROBLEM=1

if [ -n "$STALE" ]; then
    say "stalled" "$STALE"
    PROBLEM=1
fi

echo ""
"$PYTHON" "${REPO_DIR}/analyze.py" --db "$DB" --latest

echo ""
if [ "$PROBLEM" -eq 0 ]; then
    echo "OK - recording."
else
    echo "NEEDS ATTENTION - see the lines above."
fi
exit "$PROBLEM"
