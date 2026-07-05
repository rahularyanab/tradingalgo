"""
Persistent S/R level database.

Levels are stored in data/sr_levels.json and accumulate over time.
Every scan, newly discovered multi-session clusters are merged into the
database — existing levels gain more touches, new levels are added.

A level grows stronger the more sessions it has been tested on:
  WEAK:     2 unique dates  (newly confirmed, use cautiously)
  MODERATE: 3-4 unique dates
  STRONG:   5+ unique dates  (weeks of confirmation)

Broken levels (price closed clearly beyond them) are retained for history
but excluded from active signals. A broken resistance often becomes support.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Optional

from config import CLUSTER_BAND, SR_PROXIMITY_PCT

logger = logging.getLogger(__name__)

_DB_FILE = Path(__file__).parent / "sr_levels.json"


@dataclass
class SRLevel:
    level:       float
    sr_type:     str             # "resistance" | "support"
    touches:     int
    dates:       list            # ISO date strings — one entry per touch
    first_seen:  str             # ISO date string
    last_seen:   str             # ISO date string
    broken:      bool = False
    broken_date: Optional[str]  = None

    @property
    def unique_dates(self) -> int:
        return len(set(self.dates))

    @property
    def age_days(self) -> int:
        return (date.today() - date.fromisoformat(self.first_seen)).days

    @property
    def strength(self) -> str:
        ud = self.unique_dates
        if ud >= 5:
            return "STRONG"
        if ud >= 3:
            return "MODERATE"
        return "WEAK"


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> list[SRLevel]:
    if not _DB_FILE.exists():
        return []
    try:
        with open(_DB_FILE) as f:
            raw = json.load(f)
        return [SRLevel(**r) for r in raw]
    except Exception as e:
        logger.error(f"SR DB load error: {e}")
        return []


def _save(levels: list[SRLevel]) -> None:
    try:
        _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_DB_FILE, "w") as f:
            json.dump([asdict(l) for l in levels], f, indent=2, default=str)
    except Exception as e:
        logger.error(f"SR DB save error: {e}")


# ── Merge helpers ─────────────────────────────────────────────────────────────

def _find_matching(levels: list[SRLevel], price: float, sr_type: str) -> Optional[SRLevel]:
    """Return the existing level closest to price within CLUSTER_BAND, or None."""
    candidates = [
        l for l in levels
        if l.sr_type == sr_type and abs(l.level - price) <= CLUSTER_BAND and not l.broken
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda l: abs(l.level - price))


def _merge_clusters(
    existing: list[SRLevel],
    new_clusters: list[tuple[float, int, set]],   # (center, touches, date_set)
    sr_type: str,
    today: date,
) -> list[SRLevel]:
    """
    Merge newly discovered multi-session clusters into the existing database.
    new_clusters must already have >= 2 unique dates (validated by trendline.py).
    """
    today_str = today.isoformat()
    for center, _, date_set in new_clusters:
        match = _find_matching(existing, center, sr_type)
        if match:
            # Update existing level: recalculate center, add new dates
            all_dates = match.dates + [d.isoformat() if hasattr(d, "isoformat") else str(d)
                                       for d in date_set]
            new_center = (match.level * match.touches + center) / (match.touches + 1)
            match.level     = round(new_center, 1)
            match.touches   = len(all_dates)
            match.dates     = all_dates
            match.last_seen = today_str
            logger.info(
                f"SR updated: {sr_type} {match.level:.0f}  "
                f"touches={match.touches}  unique_dates={match.unique_dates}  "
                f"strength={match.strength}"
            )
        else:
            # New level
            dates = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in date_set]
            existing.append(SRLevel(
                level=round(center, 1),
                sr_type=sr_type,
                touches=len(dates),
                dates=dates,
                first_seen=min(dates),
                last_seen=today_str,
            ))
            logger.info(
                f"SR new: {sr_type} {center:.0f}  "
                f"unique_dates={len(date_set)}  dates={sorted(date_set)}"
            )
    return existing


def _mark_broken(levels: list[SRLevel], current_price: float, today: date) -> list[SRLevel]:
    """
    Mark a level broken when price has clearly crossed it (by more than half CLUSTER_BAND).
    Broken resistance often becomes support — we retain it with broken=True.
    """
    threshold = CLUSTER_BAND / 2
    today_str = today.isoformat()
    for l in levels:
        if l.broken:
            continue
        if l.sr_type == "resistance" and current_price > l.level + threshold:
            l.broken      = True
            l.broken_date = today_str
            logger.info(f"SR broken: resistance {l.level:.0f} (spot {current_price:.0f})")
        elif l.sr_type == "support" and current_price < l.level - threshold:
            l.broken      = True
            l.broken_date = today_str
            logger.info(f"SR broken: support {l.level:.0f} (spot {current_price:.0f})")
    return levels


# ── Public API ────────────────────────────────────────────────────────────────

def update_and_query(
    res_clusters: list[tuple[float, int, set]],   # from trendline.py (multi-session only)
    sup_clusters: list[tuple[float, int, set]],
    current_price: float,
    today: date,
) -> tuple[Optional[float], bool, Optional[float], bool]:
    """
    1. Load existing levels.
    2. Mark any broken levels.
    3. Merge new clusters into the database.
    4. Save.
    5. Return (resistance_level, at_resistance, support_level, at_support)
       using only active (non-broken) levels.
    """
    levels = _load()
    levels = _mark_broken(levels, current_price, today)
    levels = _merge_clusters(levels, res_clusters, "resistance", today)
    levels = _merge_clusters(levels, sup_clusters, "support", today)
    _save(levels)

    # ── Query: nearest active resistance above spot ────────────────
    active_res = [
        l for l in levels
        if l.sr_type == "resistance" and not l.broken and l.level > current_price
    ]
    resistance_level = None
    at_resistance    = False
    if active_res:
        nearest_res   = min(active_res, key=lambda l: l.level - current_price)
        resistance_level = nearest_res.level
        proximity        = (resistance_level - current_price) / resistance_level
        at_resistance    = proximity <= SR_PROXIMITY_PCT
        logger.info(
            f"DB resistance: {resistance_level:.0f}  strength={nearest_res.strength}  "
            f"unique_dates={nearest_res.unique_dates}  proximity={proximity:.2%}  "
            f"at_resistance={at_resistance}"
        )

    # ── Query: nearest active support below spot ───────────────────
    active_sup = [
        l for l in levels
        if l.sr_type == "support" and not l.broken and l.level < current_price
    ]
    support_level = None
    at_support    = False
    if active_sup:
        nearest_sup   = max(active_sup, key=lambda l: l.level)
        support_level = nearest_sup.level
        proximity     = (current_price - support_level) / support_level
        at_support    = proximity <= SR_PROXIMITY_PCT
        logger.info(
            f"DB support: {support_level:.0f}  strength={nearest_sup.strength}  "
            f"unique_dates={nearest_sup.unique_dates}  proximity={proximity:.2%}  "
            f"at_support={at_support}"
        )

    return resistance_level, at_resistance, support_level, at_support


def get_nearby_levels(
    current_price: float,
    trade_action: str,
    warning_pct: float = 0.003,
) -> list[SRLevel]:
    """
    Return active S/R levels that price is approaching and that are
    ADVERSE to the open trade — i.e., a level that could hurt the position
    if broken.

    CALL SELL → resistance above spot (if broken, price runs up → bad)
    PUT SELL  → support below spot   (if broken, price falls → bad)
    STRANGLE  → both

    warning_pct: within this % of the level = "approaching"
    """
    levels = _load()
    result = []
    for l in levels:
        if l.broken:
            continue
        if trade_action == "CALL_SELL" and l.sr_type == "resistance":
            if l.level > current_price:
                proximity = (l.level - current_price) / l.level
                if proximity <= warning_pct:
                    result.append(l)
        elif trade_action == "PUT_SELL" and l.sr_type == "support":
            if l.level < current_price:
                proximity = (current_price - l.level) / current_price
                if proximity <= warning_pct:
                    result.append(l)
        elif trade_action == "STRANGLE":
            if l.sr_type == "resistance" and l.level > current_price:
                proximity = (l.level - current_price) / l.level
                if proximity <= warning_pct:
                    result.append(l)
            elif l.sr_type == "support" and l.level < current_price:
                proximity = (current_price - l.level) / current_price
                if proximity <= warning_pct:
                    result.append(l)
    # Sort: nearest first
    return sorted(result, key=lambda l: abs(l.level - current_price))


def summary() -> str:
    """Human-readable summary of the current database (for Telegram /sr command)."""
    levels = _load()
    if not levels:
        return "SR database is empty — levels build up as the bot runs daily."

    active_res = sorted(
        [l for l in levels if l.sr_type == "resistance" and not l.broken],
        key=lambda l: l.level,
    )
    active_sup = sorted(
        [l for l in levels if l.sr_type == "support" and not l.broken],
        key=lambda l: -l.level,
    )
    broken = [l for l in levels if l.broken]

    lines = [f"*S/R DATABASE*  ({len(levels)} levels total)\n"]

    lines.append("*Resistance* (price above = bearish)")
    for l in active_res:
        lines.append(
            f"  🔴 `{l.level:.0f}`  {l.strength}  "
            f"{l.unique_dates} dates  {l.touches} touches  "
            f"(since {l.first_seen})"
        )

    lines.append("\n*Support* (price below = bullish)")
    for l in active_sup:
        lines.append(
            f"  🟢 `{l.level:.0f}`  {l.strength}  "
            f"{l.unique_dates} dates  {l.touches} touches  "
            f"(since {l.first_seen})"
        )

    if broken:
        lines.append(f"\n_Broken levels: {len(broken)} (retained for history)_")

    return "\n".join(lines)
