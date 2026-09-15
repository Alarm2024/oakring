#!/usr/bin/env python3
"""Notify when the recording breaks, and when it recovers.

Run on a timer. It only sends on a change of state - a healthy run after a
healthy run is silent - so it can check every few minutes without becoming
noise you learn to ignore.

    python3 alert.py           # check, notify if something changed
    python3 alert.py --test    # send a test message now, to prove setup works
    python3 alert.py --dry-run # print what would be sent, send nothing

Configure at least one transport in ~/.config/oakring/.env:

    ALERT_TELEGRAM_TOKEN=123456:ABC...
    ALERT_TELEGRAM_CHAT_ID=987654321
    ALERT_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
    ALERT_WEBHOOK=https://example.test/hook     # generic, posts {"text": ...}
    ALERT_REPEAT_HOURS=6                        # re-notify while still broken
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import common
import health

USER_AGENT = "oakring-alert/1.0"
STATE_NAME = "alert-state.json"
DEFAULT_REPEAT_HOURS = 6


def state_path() -> Path:
    return common.config_dir() / STATE_NAME


def load_state() -> dict:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        logging.warning("alert state unreadable, treating this as the first run")
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    temp.replace(path)
    path.chmod(0o600)


def should_notify(report: dict, state: dict, repeat_sec: int, now: float) -> tuple[bool, str]:
    """Send on a transition, on a new problem, or once the repeat window passes."""
    previous_ok = state.get("ok")
    last_sent = float(state.get("last_sent", 0))

    if previous_ok is None:
        # First ever run: say something only if it is already broken. Announcing
        # health on install would train you to ignore the channel.
        return (not report["ok"], "first run")
    if previous_ok and not report["ok"]:
        return True, "broke"
    if not previous_ok and report["ok"]:
        return True, "recovered"
    if not report["ok"]:
        if sorted(state.get("problems", [])) != sorted(report["problems"]):
            return True, "new problem"
        if now - last_sent >= repeat_sec:
            return True, "still broken"
    return False, "no change"


def transports(env: dict) -> list[tuple[str, str, dict]]:
    """(name, url, payload-template) for every configured destination."""
    configured: list[tuple[str, str, dict]] = []

    token = env.get("ALERT_TELEGRAM_TOKEN", "").strip()
    chat_id = env.get("ALERT_TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        # The token is in the URL, so this URL must never reach a log line.
        configured.append(
            ("telegram", f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": None})
        )
    elif token or chat_id:
        logging.warning("telegram needs both ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID")

    webhook = env.get("ALERT_DISCORD_WEBHOOK", "").strip()
    if webhook:
        configured.append(("discord", webhook, {"content": None}))

    generic = env.get("ALERT_WEBHOOK", "").strip()
    if generic:
        configured.append(("webhook", generic, {"text": None}))

    return configured


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret and len(secret) > 6:
            text = text.replace(secret, "***")
    return text


def _reason(exc: urllib.error.HTTPError, secrets: tuple[str, ...]) -> str:
    """The API's own explanation, which is what makes a 400 actionable.

    The body is the service's response, not our request, so it does not carry
    the token - but redact anyway in case a misconfigured endpoint echoes it.
    """
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - a missing body must not mask the real error
        return ""
    return f" - {redact(body[:300], secrets)}" if body else ""


def send(
    name: str,
    url: str,
    template: dict,
    message: str,
    timeout: int = 15,
    secrets: tuple[str, ...] = (),
) -> bool:
    """Post one message. Never logs the URL: it can carry a bot token."""
    payload = {key: (message if value is None else value) for key, value in template.items()}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        logging.info("%s notified", name)
        return True
    except urllib.error.HTTPError as exc:
        logging.error("%s rejected the message: HTTP %s%s", name, exc.code, _reason(exc, secrets))
    except Exception as exc:  # noqa: BLE001 - never let a transport fault escape
        logging.error("%s could not be reached: %s", name, type(exc).__name__)
    return False


def build_message(report: dict) -> str:
    lines = [health.summarise(report)]
    if not report["ok"]:
        lines.append("")
        for problem in report["problems"]:
            lines.append(f"- {problem}")
    lines.append("")
    lines.append(f"checked {report['checked_at']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Notify when oakring stops recording healthily.")
    parser.add_argument("--db", help="database path (default: DB_PATH from .env)")
    parser.add_argument("--stale-after", default="5m", help="tick age that counts as stale (default: 5m)")
    parser.add_argument("--test", action="store_true", help="send a test message now and exit")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")
    parser.add_argument("--force", action="store_true", help="send the current status even if nothing changed")
    args = parser.parse_args(argv)

    env = common.load_config()
    common.setup_logging(env.get("LOG_LEVEL", "INFO"))
    destinations = transports(env)
    secrets = tuple(
        env.get(key, "").strip()
        for key in ("ALERT_TELEGRAM_TOKEN", "ALERT_DISCORD_WEBHOOK", "ALERT_WEBHOOK")
    )

    if not destinations and not args.dry_run:
        print(
            "no alert destination configured - set ALERT_TELEGRAM_TOKEN and "
            "ALERT_TELEGRAM_CHAT_ID, ALERT_DISCORD_WEBHOOK, or ALERT_WEBHOOK "
            "in the .env file",
            file=sys.stderr,
        )
        return 2

    if args.test:
        message = f"oakring test message - alerts are working ({common.to_ts_utc(common.now_utc())})"
        if args.dry_run:
            print(message)
            return 0
        results = [send(*destination, message, secrets=secrets) for destination in destinations]
        return 0 if all(results) else 1

    db_path = Path(args.db) if args.db else common.db_path_from(env)
    try:
        stale_after = common.parse_duration(args.stale_after)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = health.health(db_path, stale_after)
    repeat_sec = common.env_int(env, "ALERT_REPEAT_HOURS", DEFAULT_REPEAT_HOURS, minimum=0) * 3600
    state = load_state()
    now = time.time()

    notify, reason = should_notify(report, state, repeat_sec, now)
    if args.force:
        notify, reason = True, "forced"

    if notify:
        message = build_message(report)
        if args.dry_run:
            print(f"[would send: {reason}]")
            print(message)
        else:
            delivered = [send(*destination, message, secrets=secrets) for destination in destinations]
            if any(delivered):
                state["last_sent"] = now
            else:
                # Leave last_sent alone so the next run tries again.
                logging.error("no destination accepted the alert")
    else:
        logging.info("no alert sent (%s)", reason)

    if not args.dry_run:
        state["ok"] = report["ok"]
        state["problems"] = report["problems"]
        state["checked_at"] = report["checked_at"]
        save_state(state)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
