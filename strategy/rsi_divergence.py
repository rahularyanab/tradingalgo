"""
Calculates 14-period RSI and detects bullish/bearish divergence
using a rolling lookback window rather than confirmed pivot highs.

Rolling approach: find the bar with the peak RSI in the last N bars.
If current price is at or above that bar's HIGH but RSI is weaker by
≥ DIVERGENCE_RSI_MIN_DIFF → bearish divergence (and vice versa for bullish).

This fires within one or two candles of the divergence forming, instead
of waiting 5 candles for pivot confirmation (75 min lag on a 15-min chart).
"""

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import RSI_PERIOD, PROXIMITY_PCT, DIVERGENCE_LOOKBACK, DIVERGENCE_RSI_MIN_DIFF

logger = logging.getLogger(__name__)


@dataclass
class RSIResult:
    bullish_divergence:            bool
    bearish_divergence:            bool
    bullish_divergence_developing: bool   # kept for API compat; always False now
    bearish_divergence_developing: bool   # kept for API compat; always False now
    rsi_current:       float
    rsi_prev_pivot:    float   # RSI at the reference bar (peak/trough)
    price_current:     float
    price_prev_pivot:  float   # close price at the reference bar


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def analyse_rsi_divergence(df: pd.DataFrame) -> RSIResult:
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    rsi           = _calc_rsi(close, RSI_PERIOD)
    rsi_current   = rsi.iloc[-1]
    price_current = close.iloc[-1]

    bearish_div = False
    bullish_div = False
    rsi_ref     = rsi_current
    price_ref   = price_current

    # Need at least 3 bars in window to avoid noise
    lookback = min(DIVERGENCE_LOOKBACK, len(df) - 2)
    if lookback < 3:
        return RSIResult(
            bullish_divergence=False, bearish_divergence=False,
            bullish_divergence_developing=False, bearish_divergence_developing=False,
            rsi_current=round(rsi_current, 1), rsi_prev_pivot=round(rsi_current, 1),
            price_current=price_current, price_prev_pivot=price_current,
        )

    # Exclude the current bar from the window
    window_rsi   = rsi.iloc[-(lookback + 1):-1]
    window_high  = high.iloc[-(lookback + 1):-1]
    window_low   = low.iloc[-(lookback + 1):-1]
    window_close = close.iloc[-(lookback + 1):-1]

    # ── Bearish divergence ────────────────────────────────────────
    # Reference: bar where RSI was HIGHEST in the lookback window.
    # Bearish = price now at or above that bar's high, RSI now weaker.
    rsi_peak_idx  = window_rsi.idxmax()
    rsi_peak_val  = window_rsi.loc[rsi_peak_idx]
    high_at_peak  = window_high.loc[rsi_peak_idx]
    close_at_peak = window_close.loc[rsi_peak_idx]

    if (high.iloc[-1] >= high_at_peak * (1 - PROXIMITY_PCT)
            and rsi_current < rsi_peak_val - DIVERGENCE_RSI_MIN_DIFF):
        bearish_div = True
        rsi_ref   = rsi_peak_val
        price_ref = close_at_peak
        logger.info(
            f"Bearish RSI divergence: ref high {high_at_peak:.1f} RSI {rsi_peak_val:.1f}  "
            f"→  current high {high.iloc[-1]:.1f} RSI {rsi_current:.1f}"
        )

    # ── Bullish divergence ────────────────────────────────────────
    # Reference: bar where RSI was LOWEST in the lookback window.
    # Bullish = price now at or below that bar's low, RSI now stronger.
    if not bearish_div:
        rsi_trough_idx  = window_rsi.idxmin()
        rsi_trough_val  = window_rsi.loc[rsi_trough_idx]
        low_at_trough   = window_low.loc[rsi_trough_idx]
        close_at_trough = window_close.loc[rsi_trough_idx]

        if (low.iloc[-1] <= low_at_trough * (1 + PROXIMITY_PCT)
                and rsi_current > rsi_trough_val + DIVERGENCE_RSI_MIN_DIFF):
            bullish_div = True
            rsi_ref   = rsi_trough_val
            price_ref = close_at_trough
            logger.info(
                f"Bullish RSI divergence: ref low {low_at_trough:.1f} RSI {rsi_trough_val:.1f}  "
                f"→  current low {low.iloc[-1]:.1f} RSI {rsi_current:.1f}"
            )

    return RSIResult(
        bullish_divergence=bullish_div,
        bearish_divergence=bearish_div,
        bullish_divergence_developing=False,
        bearish_divergence_developing=False,
        rsi_current=round(rsi_current, 1),
        rsi_prev_pivot=round(rsi_ref, 1),
        price_current=price_current,
        price_prev_pivot=price_ref,
    )
