"""
Telegram command listener — polls for incoming messages and responds.

Supported commands (only from the configured TELEGRAM_CHAT_ID):
  /sr                              — S/R level database
  /addsr <price> <resistance|support>    — manually add/reinforce a level
  /removesr <price> [resistance|support] — manually remove a level
  /pnl [YYYY-MM]   — daily P&L for the bot's own trades (default: this month)
  /params          — this user's settings (capital, daily limit, lots)
  /pause           — stop NEW trades and added size (open positions stay protected)
  /resume          — allow new trades again
  /status          — paused or running, and any open trade
  /pauseall, /resumeall — owner only: the same for EVERY bot on this server
  /start           — the welcome guide again
  /help            — list all commands
"""

import logging
import os
import threading
import time
from datetime import datetime

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SETTINGS

logger = logging.getLogger(__name__)

_UPDATES_URL      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
_SEND_URL         = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
_SET_COMMANDS_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"

# Populates Telegram's "/" autocomplete menu — separate from the _handle()
# dispatch below, so a new command must be added to both places.
_BOT_COMMANDS = [
    {"command": "sr",        "description": "S/R level database"},
    {"command": "addsr",     "description": "Add/reinforce a level: <price> <resistance|support>"},
    {"command": "removesr",  "description": "Remove a level: <price> [resistance|support]"},
    {"command": "pnl",       "description": "Daily P&L for bot trades (default: this month)"},
    {"command": "params",    "description": "Show your settings (capital, daily limit, lots)"},
    {"command": "pause",     "description": "Pause: no new trades (open ones stay protected)"},
    {"command": "resume",    "description": "Resume new trades"},
    {"command": "status",    "description": "Paused or running, and any open trade"},
    {"command": "pauseall",  "description": "Owner only: pause every bot on the server"},
    {"command": "resumeall", "description": "Owner only: resume every bot on the server"},
    {"command": "start",     "description": "Show the welcome guide again"},
    {"command": "help",      "description": "List all commands"},
]

_PROXY   = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
_PROXIES = {"https": _PROXY, "http": _PROXY} if _PROXY else None


def _parse_sr_type(raw: str) -> str | None:
    """'resistance'/'r' -> 'resistance', 'support'/'s' -> 'support', else None."""
    raw = raw.lower()
    if raw.startswith("r"):
        return "resistance"
    if raw.startswith("s"):
        return "support"
    return None


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

    if cmd == "/help":
        _send(chat_id, (
            "*Bot commands:*\n"
            "`/sr`       — S/R level database (all tracked levels)\n"
            "`/addsr <price> <resistance|support>` — add/reinforce a level\n"
            "`/removesr <price> [resistance|support]` — remove a level\n"
            "`/pnl [YYYY-MM]` — daily P&L for the bot's own trades (default: this month)\n"
            "`/params`   — your settings (capital, daily limit, lots)\n"
            "`/pause`    — stop NEW trades; an open trade keeps its stop-loss and closes by 3:25 PM\n"
            "`/resume`   — allow new trades again\n"
            "`/status`   — paused or running, and any open trade\n"
            + ("`/pauseall` / `/resumeall` — pause or resume EVERY bot on this server (owner)\n" if SETTINGS is None or SETTINGS.owner else "") +
            "`/start`    — the welcome guide again\n"
            "`/help`     — this message"
        ))

    elif cmd in ("/pause", "/resume", "/status", "/pauseall", "/resumeall"):
        import control
        if cmd in ("/pauseall", "/resumeall") and not (SETTINGS is None or SETTINGS.owner):
            _send(chat_id, "Only the owner can pause or resume every bot. Use /pause or /resume for your own bot.")
            return
        if cmd == "/pause":
            changed = control.pause()
            _send(chat_id, ("⏸ *Paused.*" if changed else "Already paused.") +
                  "\nNo new trades and no added size. An open trade keeps its stop-loss and is still closed by 3:25 PM.\n"
                  f"{control.open_trade_note()}\nSend /resume to continue.")
        elif cmd == "/resume":
            was_all = control.global_paused()
            changed = control.resume()
            if was_all:
                _send(chat_id, "The owner has paused ALL bots on this server; /resume does not override that. You will be told when it lifts.")
            else:
                _send(chat_id, "▶️ *Resumed.* The bot will look for new trades again at the next scan." if changed else "Not paused — nothing to resume.")
        elif cmd == "/pauseall":
            control.pause_all()
            _send(chat_id, "⏸ *ALL bots on this server are paused* (new trades only; open trades stay protected). Send /resumeall to lift it.")
        elif cmd == "/resumeall":
            _send(chat_id, "▶️ *All bots resumed* (any bot paused individually stays paused)." if control.resume_all() else "No server-wide pause was active.")
        else:
            paused, why = control.is_paused()
            _send(chat_id, (f"⏸ *PAUSED* — {why}" if paused else "▶️ *Running*") + f"\n{control.open_trade_note()}")

    elif cmd in ("/start", "/params"):
        if SETTINGS is None:
            _send(chat_id, "This bot runs without a user profile (owner mode), so there are no per-user settings to show.")
            return
        from notifications import onboarding
        if cmd == "/params":
            _send(chat_id, onboarding.params_message(SETTINGS))
        else:
            for m in onboarding.welcome_messages(SETTINGS):
                _send(chat_id, m)

    elif cmd == "/sr":
        from data.sr_database import summary as sr_summary
        _send(chat_id, sr_summary())

    elif cmd == "/addsr":
        if len(args) < 2:
            _send(chat_id, "Usage: `/addsr <price> <resistance|support>`\nExample: `/addsr 24500 resistance`")
            return
        try:
            price = float(args[0])
        except ValueError:
            _send(chat_id, f"⚠️ Bad price `{args[0]}`.")
            return
        sr_type = _parse_sr_type(args[1])
        if not sr_type:
            _send(chat_id, f"⚠️ Bad type `{args[1]}` — use `resistance`/`r` or `support`/`s`.")
            return
        from data.sr_database import add_level
        lvl = add_level(price, sr_type)
        _send(chat_id, (
            f"✅ *{sr_type.capitalize()} level added:* `{lvl.level:.0f}`\n"
            f"{lvl.strength}  {lvl.unique_dates} date(s)  {lvl.touches} touch(es)"
        ))

    elif cmd == "/removesr":
        if not args:
            _send(chat_id, "Usage: `/removesr <price> [resistance|support]`\nExample: `/removesr 24500` or `/removesr 24500 resistance`")
            return
        try:
            price = float(args[0])
        except ValueError:
            _send(chat_id, f"⚠️ Bad price `{args[0]}`.")
            return

        sr_type = None
        if len(args) > 1:
            sr_type = _parse_sr_type(args[1])
            if not sr_type:
                _send(chat_id, f"⚠️ Bad type `{args[1]}` — use `resistance`/`r` or `support`/`s`.")
                return

        from data.sr_database import find_levels_near, remove_level
        if sr_type is None:
            matches = find_levels_near(price)
            types_found = {l.sr_type for l in matches}
            if len(types_found) > 1:
                lines = [f"⚠️ Multiple levels near `{price:.0f}` — specify type:"]
                for l in matches:
                    lines.append(f"  {'🔴' if l.sr_type == 'resistance' else '🟢'} `{l.level:.0f}` {l.sr_type}")
                lines.append("Retry with `/removesr <price> resistance` or `/removesr <price> support`.")
                _send(chat_id, "\n".join(lines))
                return
            if types_found:
                sr_type = types_found.pop()

        if sr_type is None:
            _send(chat_id, f"No S/R level found near `{price:.0f}`.")
            return

        removed = remove_level(price, sr_type)
        if removed:
            _send(chat_id, f"🗑 *{sr_type.capitalize()} level removed:* `{removed.level:.0f}` (was {removed.strength}, {removed.touches} touches)")
        else:
            _send(chat_id, f"No {sr_type} level found near `{price:.0f}`.")

    elif cmd == "/pnl":
        from execution.pnl_report import monthly_summary

        now = datetime.now()
        if args:
            try:
                year, month = map(int, args[0].split("-"))
            except ValueError:
                _send(chat_id, f"⚠️ Bad month `{args[0]}`. Use `YYYY-MM`, e.g. `/pnl 2026-07`.")
                return
        else:
            year, month = now.year, now.month

        days, total, trades = monthly_summary(year, month)
        if not days:
            _send(chat_id, f"No journaled bot trades for {year}-{month:02d} yet.")
            return

        lines = [f"*Bot P&L — {year}-{month:02d}*", "━━━━━━━━━━━━━━━━━━━━"]
        for d in days:
            sign = "+" if d.net_pnl >= 0 else "−"
            n = "trade" if d.trades == 1 else "trades"
            lines.append(f"`{d.date}`  {sign}₹{abs(d.net_pnl):,.0f}  ({d.trades} {n})")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        total_sign = "+" if total >= 0 else "−"
        lines.append(f"*Total: {total_sign}₹{abs(total):,.0f} across {trades} trade(s)*")
        _send(chat_id, "\n".join(lines))

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


def _register_commands() -> None:
    """Push _BOT_COMMANDS to Telegram so they show up in the client's `/` menu."""
    try:
        resp = requests.post(
            _SET_COMMANDS_URL, json={"commands": _BOT_COMMANDS}, timeout=10, proxies=_PROXIES,
        )
        resp.raise_for_status()
        if resp.json().get("ok"):
            logger.info(f"Registered {len(_BOT_COMMANDS)} commands with Telegram.")
        else:
            logger.warning(f"setMyCommands rejected: {resp.text}")
    except Exception as e:
        logger.warning(f"setMyCommands failed: {e}")


def start_command_listener() -> None:
    """Start the Telegram command listener in a daemon thread."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing — command listener not started.")
        return
    _register_commands()
    t = threading.Thread(target=_poll_loop, daemon=True, name="tg-cmd-listener")
    t.start()
