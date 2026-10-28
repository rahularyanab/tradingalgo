"""
"Two failed entries and the day is over."

An entry attempt FAILS when the broker refuses an order (margin, rejection, API error) or accepts it and then never shows the
position. After `config.ENTRY_FAIL_LIMIT` failures on the same day this instance stops entering for the rest of that day
(control.is_paused() reports it, so every entry gate in main.py honours it; protective logic is untouched). It resets by
itself tomorrow, or immediately with /resume. A limit of 0 (the default) switches the whole feature off.

State is a tiny per-instance file so a restart cannot forget today's failures.
"""
import json
import logging
from datetime import date

import paths

logger = logging.getLogger(__name__)


def limit() -> int:
    import config
    return int(getattr(config, "ENTRY_FAIL_LIMIT", 0) or 0)


def _read(today=None) -> dict:
    today = (today or date.today()).isoformat()
    try:
        d = json.loads(paths.ENTRY_FAILS_FILE.read_text())
        if d.get("date") == today:
            return d
    except Exception:
        pass
    return {"date": today, "count": 0, "last": ""}


def count(today=None) -> int:
    return int(_read(today)["count"])


def halted_today(today=None) -> bool:
    n = limit()
    return n > 0 and count(today) >= n


def record_failure(reason: str, today=None):
    """Returns (failures_today, halted_now). Does nothing (0, False) when the feature is off."""
    if limit() <= 0:
        return 0, False
    d = _read(today)
    d["count"] += 1
    d["last"] = str(reason)[:300]
    try:
        paths.ENTRY_FAILS_FILE.write_text(json.dumps(d))
    except Exception as e:
        logger.error(f"Could not record the failed entry ({e}) — the day limit may not hold across a restart.")
    logger.warning(f"ENTRY FAILURE {d['count']}/{limit()}: {reason}")
    return d["count"], d["count"] >= limit()


def reset() -> bool:
    """Forget today's failures. Returns True if there was anything to forget."""
    had = count() > 0
    paths.ENTRY_FAILS_FILE.unlink(missing_ok=True)
    return had
