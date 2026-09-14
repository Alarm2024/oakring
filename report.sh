#!/bin/sh
# Write a timestamped cycle report into the reports directory and print its path.
#
#   ./report.sh                        # the default week view
#   ./report.sh --since 30d --bucket 4h --swing 5
#   ./report.sh --since 30d --format json
#
# Any argument is passed straight through to analyze.py. Driven by
# oakring-report.timer, but fine to run by hand.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPORT_DIR="${OAKRING_REPORT_DIR:-${HOME}/.config/oakring/reports}"
PYTHON="${PYTHON:-/usr/bin/python3}"

[ "$#" -eq 0 ] && set -- --since 7d --bucket 1h

mkdir -p "$REPORT_DIR"
chmod 700 "$REPORT_DIR"

case " $* " in
    *" --format json "*) EXT=json ;;
    *" --format csv "*)  EXT=csv ;;
    *)                   EXT=txt ;;
esac

OUT="${REPORT_DIR}/report-$(date -u +%Y-%m-%dT%H%M%SZ).${EXT}"

# Write to a temp file first so a failed run never leaves a half report behind.
if "$PYTHON" "${REPO_DIR}/analyze.py" "$@" > "${OUT}.part"; then
    mv "${OUT}.part" "$OUT"
    chmod 600 "$OUT"
    echo "$OUT"
else
    STATUS=$?
    rm -f "${OUT}.part"
    echo "report failed (analyze.py exit ${STATUS})" >&2
    exit "$STATUS"
fi
