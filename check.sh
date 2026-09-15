#!/bin/sh
# Is oakring working? One short screen, sized for a phone.
#
#   ./check.sh
#
# Exits 0 when healthy and 1 when something needs attention, so cron and
# alert.py can both use it. The verdict is the last thing printed.
set -u

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="${PYTHON:-/usr/bin/python3}"
STALE_AFTER="${STALE_AFTER:-5m}"

DB_ARGS=""
[ -n "${OAKRING_DB:-}" ] && DB_ARGS="--db ${OAKRING_DB}"

# Services are optional: the recorder may be run by hand.
if command -v systemctl >/dev/null 2>&1; then
    printf '%-10s %s\n' "recorder" "$(systemctl is-active oakring 2>/dev/null || echo 'not installed')"
    printf '%-10s %s\n' "report" "$(systemctl is-active oakring-report.timer 2>/dev/null || echo 'not installed')"
    printf '%-10s %s\n' "alerts" "$(systemctl is-active oakring-alert.timer 2>/dev/null || echo 'not installed')"
fi

# shellcheck disable=SC2086
"$PYTHON" "${REPO_DIR}/analyze.py" $DB_ARGS --latest 2>/dev/null
echo ""

# shellcheck disable=SC2086
"$PYTHON" "${REPO_DIR}/health.py" $DB_ARGS --stale-after "$STALE_AFTER"
