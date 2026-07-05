"""
Selects the best strike to sell based on OI walls and trendline levels.

Strike selection rules:
  PUT SELL  → skip nearest put support, use the NEXT support strike.
              Keep going closer to ATM (higher strike) until LTP > Rs.30.
  CALL SELL → skip nearest call resistance, use the NEXT resistance strike.
              Keep going closer to ATM (lower strike) until LTP > Rs.30.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config import (
    UNDERLYING,
    STRIKE_STEP,
    PCR_BULLISH,
    PCR_BEARISH,
    TARGET_DECAY_PCT,
    HEDGE_PRICE_MIN,
    HEDGE_PRICE_MAX,
    HEDGE_PRICE_TARGET,
    COI_NEARBY_STRIKES,
    COI_NEARBY_FRACTION,
    MAX_OTM_STRIKES,
)
from data.option_chain import OptionChainData, StrikeData, TRADEABLE_LTP_MIN

logger = logging.getLogger(__name__)


@dataclass
class OptionSignal:
    call_bearish:     bool
    put_bullish:      bool
    best_call_strike: Optional[int]
    best_put_strike:  Optional[int]
    call_symbol:      Optional[str]
    put_symbol:       Optional[str]
    call_ltp:         Optional[float]
    put_ltp:          Optional[float]
    pcr:              float
    change_pcr:       float
    call_wall:        int
    put_wall:         int
    max_pain:         int
    top_2_calls:      list   # [StrikeData, ...]
    top_2_puts:       list   # [StrikeData, ...]
    # COI-based writing signals (independent of PCR)
    call_writing_bearish: bool = False  # fresh call OI building at near-ATM strikes
    put_writing_bullish:  bool = False  # fresh put OI building at near-ATM strikes
    # Hedge legs — far OTM options to BUY for margin reduction
    hedge_call_strike: Optional[int]   = None   # far OTM CE to buy (~₹5-6)
    hedge_call_symbol: Optional[str]   = None
    hedge_call_ltp:    Optional[float] = None
    hedge_put_strike:  Optional[int]   = None   # far OTM PE to buy (~₹5-6)
    hedge_put_symbol:  Optional[str]   = None
    hedge_put_ltp:     Optional[float] = None


def find_hedge_strike(
    oc: OptionChainData,
    option_type: str,
    sold_strike: int,
) -> Optional[StrikeData]:
    """
    Find the far OTM option with LTP closest to ₹5-6 for hedging.
    CE hedge: strike ABOVE sold CE (further OTM).
    PE hedge: strike BELOW sold PE (further OTM).
    """
    if option_type == "CE":
        candidates = [
            s for s in oc.call_data.values()
            if s.strike > sold_strike and HEDGE_PRICE_MIN <= s.ltp <= HEDGE_PRICE_MAX
        ]
    else:
        candidates = [
            s for s in oc.put_data.values()
            if s.strike < sold_strike and HEDGE_PRICE_MIN <= s.ltp <= HEDGE_PRICE_MAX
        ]

    if not candidates:
        logger.debug(f"No hedge strike found for {option_type} beyond {sold_strike}")
        return None

    best = min(candidates, key=lambda s: abs(s.ltp - HEDGE_PRICE_TARGET))
    logger.info(f"Hedge {option_type}: strike={best.strike}  ltp=₹{best.ltp}")
    return best


def _build_nfo_symbol(strike: int, option_type: str, expiry_str: str) -> str:
    """
    Build Zerodha NFO symbol for a Nifty weekly option.
    NSE expiry format: "19-Jun-2026"
    Weekly symbol: NIFTY{YY}{M}{DD}{STRIKE}{TYPE}
    Monthly symbol (fallback): NIFTY{YY}{MON}{STRIKE}{TYPE}
    """
    MONTH_MAP = {
        "Jan": "1", "Feb": "2", "Mar": "3", "Apr": "4",
        "May": "5", "Jun": "6", "Jul": "7", "Aug": "8",
        "Sep": "9", "Oct": "O", "Nov": "N", "Dec": "D",
    }
    try:
        parts = expiry_str.replace("-", " ").split()
        day_s  = parts[0].zfill(2)
        mon_s  = parts[1][:3].capitalize()
        year_s = parts[2]
        yy = year_s[-2:]
        m  = MONTH_MAP.get(mon_s, mon_s[:1])
        return f"{UNDERLYING}{yy}{m}{day_s}{strike}{option_type}"
    except Exception:
        try:
            dt  = datetime.strptime(expiry_str, "%d-%b-%Y")
            return f"{UNDERLYING}{dt.strftime('%y')}{dt.strftime('%b').upper()}{strike}{option_type}"
        except Exception:
            return f"{UNDERLYING}{strike}{option_type}"


def _select_put_strike(
    oc: OptionChainData,
    trendline_support: Optional[float] = None,
) -> tuple[Optional[int], Optional[float]]:
    """
    Returns (strike, ltp).
    Only consider puts within MAX_OTM_STRIKES (300 pts) of spot.
    If trendline_support is given, ceiling the selection at the first strike
    at-or-below that level so we never sell ABOVE the support we're defending.
    """
    spot        = oc.spot_price
    min_strike  = spot - MAX_OTM_STRIKES * STRIKE_STEP

    # Anchor: sell AT or BELOW trendline support, not above it.
    if trendline_support:
        max_strike = math.floor(trendline_support / STRIKE_STEP) * STRIKE_STEP
        max_strike = min(max_strike, int(spot) - STRIKE_STEP)   # must still be OTM
    else:
        max_strike = int(spot) - STRIKE_STEP

    candidates = sorted(
        [
            s for s in oc.put_data.values()
            if s.strike <= max_strike
            and s.strike >= min_strike
            and s.ltp >= TRADEABLE_LTP_MIN
        ],
        key=lambda s: s.strike,
        reverse=True,
    )

    if not candidates:
        candidates = sorted(
            [
                s for s in oc.put_data.values()
                if s.strike <= max_strike
                and s.strike >= min_strike
                and s.ltp > 10
            ],
            key=lambda s: s.ltp,
            reverse=True,
        )
        if not candidates:
            logger.warning(
                f"PUT SELL: no tradeable strike in [{min_strike:.0f}, {max_strike}] "
                f"(support={trendline_support})"
            )
            return None, None
        best = candidates[0]
        logger.info(f"PUT SELL (relaxed): {best.strike} PE @ ₹{best.ltp}")
        return best.strike, best.ltp

    # Among candidates at-or-below support, pick the NEAREST (highest) strike — closest to ATM.
    best = candidates[0]
    logger.info(
        f"PUT SELL strike: {best.strike} PE @ ₹{best.ltp}  "
        f"(support ceiling={max_strike})"
    )
    return best.strike, best.ltp


def _select_call_strike(
    oc: OptionChainData,
    trendline_resistance: Optional[float] = None,
) -> tuple[Optional[int], Optional[float]]:
    """
    Returns (strike, ltp).
    Only consider calls within MAX_OTM_STRIKES (300 pts) of spot.
    If trendline_resistance is given, floor the selection at the first strike
    at-or-above that level so we never sell BELOW the resistance we're defending.
    """
    spot       = oc.spot_price
    max_strike = spot + MAX_OTM_STRIKES * STRIKE_STEP

    # Anchor: sell AT or ABOVE trendline resistance, not below it.
    # Round resistance UP to the nearest strike step.
    if trendline_resistance:
        min_strike = math.ceil(trendline_resistance / STRIKE_STEP) * STRIKE_STEP
        min_strike = max(min_strike, int(spot) + STRIKE_STEP)   # must still be OTM
    else:
        min_strike = int(spot) + STRIKE_STEP

    candidates = sorted(
        [
            s for s in oc.call_data.values()
            if s.strike >= min_strike
            and s.strike <= max_strike
            and s.ltp >= TRADEABLE_LTP_MIN
        ],
        key=lambda s: s.strike,
    )

    if not candidates:
        # Relax: drop ltp floor, keep strike floor
        candidates = sorted(
            [
                s for s in oc.call_data.values()
                if s.strike >= min_strike
                and s.strike <= max_strike
                and s.ltp > 10
            ],
            key=lambda s: s.ltp,
            reverse=True,
        )
        if not candidates:
            logger.warning(
                f"CALL SELL: no tradeable strike in [{min_strike}, {max_strike:.0f}] "
                f"(resistance={trendline_resistance})"
            )
            return None, None
        best = candidates[0]
        logger.info(f"CALL SELL (relaxed): {best.strike} CE @ ₹{best.ltp}")
        return best.strike, best.ltp

    # Among candidates above resistance, pick the NEAREST (lowest) strike — closest to ATM
    # so premium is still meaningful, but we're at least at the resistance level.
    best = candidates[0]
    logger.info(
        f"CALL SELL strike: {best.strike} CE @ ₹{best.ltp}  "
        f"(resistance floor={min_strike})"
    )
    return best.strike, best.ltp


def analyse_option_signal(
    oc: OptionChainData,
    trendline_resistance: Optional[float],
    trendline_support: Optional[float],
) -> OptionSignal:

    # ── Directional confirmation ──────────────────────────────────
    call_bearish = oc.pcr < PCR_BEARISH
    put_bullish  = oc.pcr > PCR_BULLISH

    # ── COI writing signal: fresh positioning at near-ATM strikes ─
    # Call writing bearish: call sellers piling in just above spot.
    # Put writing bullish: put sellers piling in just below spot.
    spot = oc.spot_price
    atm  = round(spot / STRIKE_STEP) * STRIKE_STEP

    nearby_call_strikes = [atm + n * STRIKE_STEP for n in range(COI_NEARBY_STRIKES + 1)]
    nearby_call_coi = sum(
        max(oc.call_data[s].change_oi, 0)
        for s in nearby_call_strikes if s in oc.call_data
    )
    total_call_coi = sum(max(s.change_oi, 0) for s in oc.call_data.values()) or 1
    call_writing_bearish = (nearby_call_coi / total_call_coi) >= COI_NEARBY_FRACTION

    nearby_put_strikes = [atm - n * STRIKE_STEP for n in range(COI_NEARBY_STRIKES + 1)]
    nearby_put_coi = sum(
        max(oc.put_data[s].change_oi, 0)
        for s in nearby_put_strikes if s in oc.put_data
    )
    total_put_coi = sum(max(s.change_oi, 0) for s in oc.put_data.values()) or 1
    put_writing_bullish = (nearby_put_coi / total_put_coi) >= COI_NEARBY_FRACTION

    if call_writing_bearish:
        logger.info(
            f"Call writing signal: nearby COI {nearby_call_coi:,.0f} / total {total_call_coi:,.0f} "
            f"= {nearby_call_coi/total_call_coi:.1%}  strikes={nearby_call_strikes}"
        )
    if put_writing_bullish:
        logger.info(
            f"Put writing signal: nearby COI {nearby_put_coi:,.0f} / total {total_put_coi:,.0f} "
            f"= {nearby_put_coi/total_put_coi:.1%}  strikes={nearby_put_strikes}"
        )

    # ── Strike selection ──────────────────────────────────────────
    best_call, call_ltp = _select_call_strike(oc, trendline_resistance)
    best_put,  put_ltp  = _select_put_strike(oc, trendline_support)

    call_symbol = _build_nfo_symbol(best_call, "CE", oc.weekly_expiry_date) if best_call else None
    put_symbol  = _build_nfo_symbol(best_put,  "PE", oc.weekly_expiry_date) if best_put  else None

    # ── Hedge legs (far OTM buy ~₹5-6 for margin reduction) ──────
    hedge_call = find_hedge_strike(oc, "CE", best_call) if best_call else None
    hedge_put  = find_hedge_strike(oc, "PE", best_put)  if best_put  else None

    hedge_call_symbol = _build_nfo_symbol(hedge_call.strike, "CE", oc.weekly_expiry_date) if hedge_call else None
    hedge_put_symbol  = _build_nfo_symbol(hedge_put.strike,  "PE", oc.weekly_expiry_date) if hedge_put  else None

    logger.info(
        f"Option signal: pcr={oc.pcr} change_pcr={oc.change_pcr} "
        f"call_bearish={call_bearish} call_writing={call_writing_bearish} "
        f"put_bullish={put_bullish} put_writing={put_writing_bullish} "
        f"best_call={best_call}@{call_ltp} best_put={best_put}@{put_ltp} "
        f"hedge_call={hedge_call.strike if hedge_call else None}@{hedge_call.ltp if hedge_call else None} "
        f"hedge_put={hedge_put.strike if hedge_put else None}@{hedge_put.ltp if hedge_put else None}"
    )

    return OptionSignal(
        call_bearish=call_bearish,
        put_bullish=put_bullish,
        call_writing_bearish=call_writing_bearish,
        put_writing_bullish=put_writing_bullish,
        best_call_strike=best_call,
        best_put_strike=best_put,
        call_symbol=call_symbol,
        put_symbol=put_symbol,
        call_ltp=call_ltp,
        put_ltp=put_ltp,
        pcr=oc.pcr,
        change_pcr=oc.change_pcr,
        call_wall=oc.call_wall,
        put_wall=oc.put_wall,
        max_pain=oc.max_pain,
        top_2_calls=oc.top_2_calls,
        top_2_puts=oc.top_2_puts,
        hedge_call_strike=hedge_call.strike if hedge_call else None,
        hedge_call_symbol=hedge_call_symbol,
        hedge_call_ltp=hedge_call.ltp if hedge_call else None,
        hedge_put_strike=hedge_put.strike if hedge_put else None,
        hedge_put_symbol=hedge_put_symbol,
        hedge_put_ltp=hedge_put.ltp if hedge_put else None,
    )
