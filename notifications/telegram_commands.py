"""
Telegram command listener — polls for incoming messages and responds.

Supported commands (only from the configured TELEGRAM_CHAT_ID):
  /logs [n]   — last n lines of the log file (default 30, max 60)
  /errors     — recent ERROR/EXCEPTION lines from the log
  /status     — last scan time, market open status
  /help       — list all commands
"""

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_UPDATES_URL   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
_SEND_URL      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

_PROXY   = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
_PROXIES = {"https": _PROXY, "http": _PROXY} if _PROXY else None
_MAX_LOG_LINES = 60
_DEFAULT_LINES = 30

# Detect which log file is in use (paper vs live)
_LOG_CANDIDATES = ["logs/paper_trade.log", "logs/bot.log"]


def _log_path() -> Path | None:
    for candidate in _LOG_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def _tail(path: Path, n: int) -> str:
    """Return last n lines of a file efficiently."""
    lines = deque(path.read_text(errors="replace").splitlines(), maxlen=n)
    return "\n".join(lines)


def _send(chat_id: str, text: str, plain: bool = False) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    if len(text) > 4000:
        text = "..." + text[-3997:]
    payload: dict = {"chat_id": chat_id, "text": text}
    if not plain:
        payload["parse_mode"] = "Markdown"
    try:
        requests.post(_SEND_URL, json=payload, timeout=10, proxies=_PROXIES)
    except Exception as e:
        logger.warning(f"Command reply failed: {e}")


def _handle(text: str, chat_id: str) -> None:
    text = text.strip()
    cmd  = text.split()[0].lower().split("@")[0]   # strip @botname suffix
    args = text.split()[1:]

    log = _log_path()

    if cmd == "/help":
        _send(chat_id, (
            "*Bot commands:*\n"
            "`/logs [n]` — last n log lines (default 30, max 60)\n"
            "`/errors`   — recent ERROR / EXCEPTION lines\n"
            "`/status`   — last scan time + market status\n"
            "`/sr`       — S/R level database (all tracked levels)\n"
            "`/help`     — this message"
        ))

    elif cmd == "/logs":
        if not log:
            _send(chat_id, "⚠️ Log file not found yet.")
            return
        try:
            n = min(int(args[0]), _MAX_LOG_LINES) if args else _DEFAULT_LINES
        except ValueError:
            n = _DEFAULT_LINES
        lines = _tail(log, n)
        _send(chat_id, lines, plain=True)

    elif cmd == "/errors":
        if not log:
            _send(chat_id, "⚠️ Log file not found yet.")
            return
        all_lines = log.read_text(errors="replace").splitlines()
        # last 300 lines, filter for ERROR/EXCEPTION/Traceback
        recent = all_lines[-300:]
        error_lines = [
            l for l in recent
            if any(kw in l for kw in ("ERROR", "EXCEPTION", "Traceback", "exception"))
        ]
        if not error_lines:
            _send(chat_id, "✅ No errors in the last 300 log lines.")
        else:
            out = "\n".join(error_lines[-40:])
            _send(chat_id, out, plain=True)

    elif cmd == "/status":
        if not log:
            _send(chat_id, "⚠️ Log file not found yet.")
            return
        all_lines = log.read_text(errors="replace").splitlines()
        # Find the last scan line
        scan_line = next(
            (l for l in reversed(all_lines) if "Scan @" in l), None
        )
        signal_line = next(
            (l for l in reversed(all_lines) if "Signal:" in l), None
        )
        now = datetime.now()
        market_open = (
            now.weekday() < 5
            and (9 * 60 + 15) <= (now.hour * 60 + now.minute) <= (15 * 60 + 30)
        )
        market_str = "🟢 Market OPEN" if market_open else "🔴 Market CLOSED"

        lines = [
            f"*Bot Status* | {now.strftime('%H:%M IST %d %b')}",
            "━━━━━━━━━━━━━━━━━━━━",
            market_str,
            f"Log: `{log}`",
        ]
        if scan_line:
            lines.append(f"Last scan : `{scan_line[:80]}`")
        if signal_line:
            lines.append(f"Last signal: `{signal_line[:80]}`")
        _send(chat_id, "\n".join(lines))

    elif cmd == "/sr":
        from data.sr_database import summary as sr_summary
        _send(chat_id, sr_summary())

    else:
        _send(chat_id, f"Unknown command: `{cmd}`\nSend /help for the list.")


def _poll_loop() -> None:
    offset = None
    logger.info("Telegram command listener started.")
    while True:
        try:
            params: dict = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(_UPDATES_URL, params=params, timeout=40, proxies=_PROXIES)
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                text   = msg.get("text", "")
                chat   = str(msg.get("chat", {}).get("id", ""))

                # Reject messages from any other chat
                if chat != str(TELEGRAM_CHAT_ID):
                    continue
                if text.startswith("/"):
                    _handle(text, chat)

        except Exception as e:
            logger.warning(f"Command poll error: {e}")
            time.sleep(5)


def start_command_listener() -> None:
    """Start the Telegram command listener in a daemon thread."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing — command listener not started.")
        return
    t = threading.Thread(target=_poll_loop, daemon=True, name="tg-cmd-listener")
    t.start()
