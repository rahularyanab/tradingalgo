"""
Identifies intraday S/R clusters and maintains a persistent level database.

Every scan:
  1. Find pivot highs/lows from the last 100 candles (2-bar lookback = 30 min confirm).
  2. Cluster nearby pivots. Only clusters with pivots from >= 2 different calendar
     dates are considered genuine S/R (same-session pivots are just price movement).
  3. Merge discovered clusters into data/sr_levels.json — levels accumulate
     touches over weeks/months and grow progressively stronger.
  4. at_resistance / at_support are set from the DATABASE, not from today's candles.
     This means a resistance level at 24,000 fires every time price approaches it,
     whether that's the 2nd or the 20th test — the level is proven.

Why this beats same-session trendlines:
  - Trendlines need 3 confirmed pivots with 5-bar lag each → fires 3+ hours late.
  - Same-session clusters are just price movement, not genuine rejection history.
  - Database levels have real multi-day rejection history behind them.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from config import PIVOT_LOOKBACK, CLUSTER_BAND, CLUSTER_MIN_TOUCHES, SR_PROXIMITY_PCT
from data.sr_database import update_and_query

logger = logging.getLogger(__name__)


@dataclass
class TrendlineResult:
    at_resistance:    bool
    at_support:       bool
    resistance_level: Optional[float]
    support_level:    Optional[float]
    resistance_slope: Optional[float]   # always None — kept for API compat
    support_slope:    Optional[float]   # always None — kept for API compat


def _find_pivot_highs(df: pd.DataFrame, n: int) -> list[tuple[float, date]]:
    highs  = df["high"]
    pivots = []
    for i in range(n, len(df) - n):
        window = highs.iloc[i - n: i + n + 1]
        if highs.iloc[i] == window.max() and list(window).count(highs.iloc[i]) == 1:
            ts = df.index[i]
            pivots.append((float(highs.iloc[i]), ts.date() if hasattr(ts, "date") else ts))
    return pivots


def _find_pivot_lows(df: pd.DataFrame, n: int) -> list[tuple[float, date]]:
    lows   = df["low"]
    pivots = []
    for i in range(n, len(df) - n):
        window = lows.iloc[i - n: i + n + 1]
        if lows.iloc[i] == window.min() and list(window).count(lows.iloc[i]) == 1:
            ts = df.index[i]
            pivots.append((float(lows.iloc[i]), ts.date() if hasattr(ts, "date") else ts))
    return pivots


def _cluster_multi_session(
    pivots: list[tuple[float, date]],
    band: float,
    min_touches: int,
) -> list[tuple[float, int, set]]:
    """
    Group nearby pivots into clusters.
    Only returns clusters whose touches span >= 2 distinct calendar dates.
    Returns [(center_price, touch_count, date_set), ...] sorted by touch_count desc.
    """
    if not pivots:
        return []
    sorted_pivots = sorted(pivots, key=lambda x: x[0])
    clusters = []
    i = 0
    while i < len(sorted_pivots):
        group_prices = [sorted_pivots[i][0]]
        group_dates  = {sorted_pivots[i][1]}
        j = i + 1
        while j < len(sorted_pivots) and sorted_pivots[j][0] - group_prices[0] <= band:
            group_prices.append(sorted_pivots[j][0])
            group_dates.add(sorted_pivots[j][1])
            j += 1
        center  = sum(group_prices) / len(group_prices)
        touches = len(group_prices)
        if touches >= min_touches and len(group_dates) >= 2:
            clusters.append((center, touches, group_dates))
        i = j
    return sorted(clusters, key=lambda x: -x[1])


def analyse_trendlines(df: pd.DataFrame) -> TrendlineResult:
    n             = PIVOT_LOOKBACK
    current_price = df["close"].iloc[-1]
    today         = df.index[-1].date() if hasattr(df.index[-1], "date") else date.today()

    pivot_highs = _find_pivot_highs(df, n)
    pivot_lows  = _find_pivot_lows(df, n)

    logger.debug(
        f"Pivots: {len(pivot_highs)} highs across "
        f"{len({d for _, d in pivot_highs})} dates  |  "
        f"{len(pivot_lows)} lows across "
        f"{len({d for _, d in pivot_lows})} dates"
    )

    # Only multi-session clusters feed the database
    res_clusters = _cluster_multi_session(
        [(p, d) for p, d in pivot_highs if p > current_price],
        CLUSTER_BAND, CLUSTER_MIN_TOUCHES,
    )
    sup_clusters = _cluster_multi_session(
        [(p, d) for p, d in pivot_lows if p < current_price],
        CLUSTER_BAND, CLUSTER_MIN_TOUCHES,
    )

    # Update database and get signal levels (at_resistance/at_support from DB history)
    resistance_level, at_resistance, support_level, at_support = update_and_query(
        res_clusters=res_clusters,
        sup_clusters=sup_clusters,
        current_price=current_price,
        today=today,
    )

    # Fallback display: if DB has no level yet, show nearest single-session pivot
    # (not counted for score — just for context in Telegram message)
    if resistance_level is None:
        highs_above = [p for p, _ in pivot_highs if p > current_price]
        if highs_above:
            resistance_level = min(highs_above)

    if support_level is None:
        lows_below = [p for p, _ in pivot_lows if p < current_price]
        if lows_below:
            support_level = max(lows_below)

    return TrendlineResult(
        at_resistance=at_resistance,
        at_support=at_support,
        resistance_level=resistance_level,
        support_level=support_level,
        resistance_slope=None,
        support_slope=None,
    )
