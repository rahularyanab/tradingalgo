"""
Pause / resume — the "something looks wrong, stop it" switch.

PAUSED means: no new trades, no size added to an open trade, no re-entry after a reversal.
It NEVER blocks protection: stop-loss, the day-loss floor, signal/profit exits, the 3:25 flatten and the fill
checks all keep running, so pausing can't strand an open position.

Two switches, both plain files so they survive restarts and can be read cheaply every scan:
    paths.PAUSE_FILE         this instance only         — /pause, /resume
    paths.GLOBAL_PAUSE_FILE  every instance on the box  — /pauseall, /resumeall   (owner only)
"""
import json
import logging
from datetime import datetime

import paths

logger = logging.getLogger(__name__)


def _write(path, by: str) -> None:
    path.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "by": by}))


def _read(path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"at": "", "by": "unknown"}


def _when(info: dict) -> str:
    try:
        return datetime.fromisoformat(info["at"]).strftime("%d %b %H:%M")
    except Exception:
        return "earlier"


def is_paused():
    """(paused, reason). Reason is a short human sentence, '' when running."""
    if paths.GLOBAL_PAUSE_FILE.exists():
        i = _read(paths.GLOBAL_PAUSE_FILE)
        return True, f"paused for ALL bots by the owner ({_when(i)})"
    if paths.PAUSE_FILE.exists():
        i = _read(paths.PAUSE_FILE)
        return True, f"paused by you ({_when(i)})"
    return False, ""


def global_paused() -> bool:
    return paths.GLOBAL_PAUSE_FILE.exists()


def pause(by: str = "telegram") -> bool:
    """Pause this bot. Returns False if it already was."""
    was = paths.PAUSE_FILE.exists()
    _write(paths.PAUSE_FILE, by)
    logger.warning(f"PAUSED by {by} — no new entries or additions until /resume (exits and stops keep running).")
    return not was


def resume() -> bool:
    """Resume this bot's own pause. Returns False if it wasn't paused."""
    was = paths.PAUSE_FILE.exists()
    paths.PAUSE_FILE.unlink(missing_ok=True)
    logger.warning("RESUMED by telegram — entries allowed again.")
    return was


def pause_all(by: str = "owner") -> bool:
    was = paths.GLOBAL_PAUSE_FILE.exists()
    _write(paths.GLOBAL_PAUSE_FILE, by)
    logger.warning(f"PAUSE-ALL set by {by}: every bot on this server stops entering.")
    return not was


def resume_all() -> bool:
    was = paths.GLOBAL_PAUSE_FILE.exists()
    paths.GLOBAL_PAUSE_FILE.unlink(missing_ok=True)
    logger.warning("PAUSE-ALL cleared.")
    return was


def open_trade_note() -> str:
    """One line about any open trade, from the state file the bot saves every scan (no import of main — avoids a cycle)."""
    try:
        t = json.loads(paths.STATE_FILE.read_text()).get("trade")
    except Exception:
        return ""
    if not t:
        return "No trade is open."
    from config import NIFTY_LOT_SIZE
    lots = int(t.get("lots", 0)) // NIFTY_LOT_SIZE
    return f"Open: {t['action'].replace('_', ' ')} {t['strike']} ({lots} lot{'s' if lots != 1 else ''}) — still managed: stop-loss, exits and the 3:25 flatten run as usual."
