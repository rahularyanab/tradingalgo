"""
Persisted pause-list for the whole-account MTM guard (config.TOTAL_MTM_MAX_LOSS).

Backed by a JSON file (not just an in-memory set) so a pause set via the
Telegram /pausemtm command survives a bot restart.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PAUSE_FILE = Path(__file__).parent.parent / "logs" / "mtm_guard_pause.json"


def _load() -> set[str]:
    if not _PAUSE_FILE.exists():
        return set()
    try:
        return set(json.loads(_PAUSE_FILE.read_text()))
    except Exception as e:
        logger.warning(f"mtm_guard: failed to read pause file: {e}")
        return set()


def _save(dates: set[str]) -> None:
    _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PAUSE_FILE.write_text(json.dumps(sorted(dates)))


def is_paused(date_str: str) -> bool:
    return date_str in _load()


def pause_date(date_str: str) -> None:
    dates = _load()
    dates.add(date_str)
    _save(dates)


def resume_all() -> set[str]:
    """Clear every paused date. Returns the dates that were cleared."""
    cleared = _load()
    _save(set())
    return cleared


def paused_dates() -> set[str]:
    return _load()
