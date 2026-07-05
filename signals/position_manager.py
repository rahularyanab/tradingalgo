"""
Active position management — evaluates every 15-min candle while in a trade.

Rules:
  STRANGLE
    Bullish ≥2/3 → exit CE leg (going ITM), let PE decay to zero
    Bearish ≥2/3 → exit PE leg (going ITM), let CE decay to zero

  CALL SELL (bearish position)
    Opposing bullish 3/3 → exit + enter PUT SELL (ride the reversal)
    Opposing bullish 2/3 → exit + enter STRANGLE (trend unclear)

  PUT SELL (bullish position)
    Opposing bearish 3/3 → exit + enter CALL SELL (ride the reversal)
    Opposing bearish 2/3 → exit + enter STRANGLE (trend unclear)

  Developing divergence alone → WARNING only, no exit yet
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (PCR_BULLISH, PCR_BEARISH, MIN_SIGNAL_SCORE,
                    ROLL_THRESHOLD_PTS, MAX_ROLLS_PER_DAY,
                    BREAKOUT_CONFIRM_CANDLES, REVERSAL_CONFIRM_CANDLES, CLEAN_CONFIRM_CANDLES,
                    PARTIAL_PROFIT_LOCK_PNL, NIFTY_LOT_SIZE)
from strategy.trendline import TrendlineResult
from strategy.rsi_divergence import RSIResult
from strategy.option_signal import OptionSignal
from signals.trade_monitor import TradeState

logger = logging.getLogger(__name__)

REVERSAL_THRESHOLD   = 3   # score needed to fully reverse into opposite directional trade
SWITCH_THRESHOLD     = 3   # raised from 2 → 3: 2/3 opposing signals now just HOLDs,
                           # avoids excessive CALL↔PUT churn when market is ranging


@dataclass
class StrangleLegState:
    """Tracks which legs of a strangle are still open."""
    ce_strike:        int
    ce_symbol:        str
    ce_entry_premium: float
    pe_strike:        int
    pe_symbol:        str
    pe_entry_premium: float
    ce_active:        bool = True
    pe_active:        bool = True

    @property
    def is_fully_closed(self):
        return not self.ce_active and not self.pe_active

    @property
    def remaining_leg(self):
        if self.ce_active and not self.pe_active:
            return "CE"
        if self.pe_active and not self.ce_active:
            return "PE"
        return None


@dataclass
class ManagementDecision:
    action:     str            # see ACTION_* constants below
    reason:     str
    score:      int            # signal score that triggered this
    new_action: Optional[str]  = None   # "CALL_SELL"|"PUT_SELL"|"STRANGLE" after exit
    reasons:    list           = field(default_factory=list)


@dataclass
class RollContext:
    put_wall:               int
    call_wall:              int
    entry_put_wall:         Optional[int]
    entry_call_wall:        Optional[int]
    above_resistance_count: int
    below_support_count:    int
    rolls_today:            int
    roll_allowed:           bool


# ── Action constants ──────────────────────────────────────────────────────────
HOLD              = "HOLD"
EXIT_CE_LEG       = "EXIT_CE_LEG"       # strangle: buy back call, hold put
EXIT_PE_LEG       = "EXIT_PE_LEG"       # strangle: buy back put, hold call
EXIT_FULL         = "EXIT_FULL"         # close everything
REVERSE_CALL_SELL = "REVERSE_CALL_SELL" # exit + enter call sell
REVERSE_PUT_SELL  = "REVERSE_PUT_SELL"  # exit + enter put sell
SWITCH_STRANGLE   = "SWITCH_STRANGLE"   # exit + enter strangle
ROLL_UP              = "ROLL_UP"              # PUT_SELL: roll to higher strike
ROLL_DOWN            = "ROLL_DOWN"            # CALL_SELL: roll to lower strike
ADD_HEDGE_LEG        = "ADD_HEDGE_LEG"        # add opposite leg (convert to hedged strangle)
REMOVE_HEDGE_LEG     = "REMOVE_HEDGE_LEG"     # remove hedge leg (revert to directional)
PARTIAL_PROFIT_LOCK  = "PARTIAL_PROFIT_LOCK"  # exit all-but-1-lot, leave 1 running free


def bullish_score(tl: TrendlineResult, rsi: RSIResult, opt: OptionSignal) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if tl.at_support:
        score += 1
        reasons.append(f"Price at trendline support {tl.support_level:.0f}")
    if rsi.bullish_divergence:
        score += 1
        reasons.append(f"Bullish RSI divergence confirmed (RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current})")
    elif rsi.bullish_divergence_developing:
        score += 1
        reasons.append(f"Bullish RSI divergence developing (RSI {rsi.rsi_current:.1f})")
    if opt.put_bullish:
        score += 1
        reasons.append(f"PCR {opt.pcr} bullish | Put wall at {opt.put_wall}")
    return score, reasons


def bearish_score(tl: TrendlineResult, rsi: RSIResult, opt: OptionSignal) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if tl.at_resistance:
        score += 1
        reasons.append(f"Price at trendline resistance {tl.resistance_level:.0f}")
    if rsi.bearish_divergence:
        score += 1
        reasons.append(f"Bearish RSI divergence confirmed (RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current})")
    elif rsi.bearish_divergence_developing:
        score += 1
        reasons.append(f"Bearish RSI divergence developing (RSI {rsi.rsi_current:.1f})")
    if opt.call_bearish:
        score += 1
        reasons.append(f"PCR {opt.pcr} bearish | Call wall at {opt.call_wall}")
    return score, reasons


def evaluate_position(
    trade:                   TradeState,
    tl:                      TrendlineResult,
    rsi:                     RSIResult,
    opt:                     OptionSignal,
    strangle_legs:           Optional[StrangleLegState] = None,
    reversal_candle_count:   int  = 0,
    clean_after_hedge_count: int  = 0,
    hedge_active:            bool = False,
    roll_ctx:                Optional[RollContext] = None,
    current_ltp:             Optional[float] = None,
) -> ManagementDecision:
    """
    Evaluate what to do with the current position on this candle.
    Returns a ManagementDecision. Caller acts on it.
    """
    bull_score, bull_reasons = bullish_score(tl, rsi, opt)
    bear_score, bear_reasons = bearish_score(tl, rsi, opt)

    # ── STRANGLE management ───────────────────────────────────────
    if trade.action == "STRANGLE":
        legs = strangle_legs

        if legs and legs.ce_active and legs.pe_active:
            # Both legs open — check if one side is threatened
            if bull_score >= SWITCH_THRESHOLD and bull_score > bear_score:
                logger.info(
                    f"STRANGLE bullish {bull_score}/3: exit CE leg, let PE decay. "
                    f"Reasons: {bull_reasons}"
                )
                return ManagementDecision(
                    action=EXIT_CE_LEG,
                    reason="Bullish signal — CE leg at risk (market going UP). Exiting call, holding put.",
                    score=bull_score,
                    reasons=bull_reasons,
                )
            if bear_score >= SWITCH_THRESHOLD and bear_score > bull_score:
                logger.info(
                    f"STRANGLE bearish {bear_score}/3: exit PE leg, let CE decay. "
                    f"Reasons: {bear_reasons}"
                )
                return ManagementDecision(
                    action=EXIT_PE_LEG,
                    reason="Bearish signal — PE leg at risk (market going DOWN). Exiting put, holding call.",
                    score=bear_score,
                    reasons=bear_reasons,
                )

        elif legs and legs.remaining_leg == "CE":
            # Only CE (call) remains — if bearish confirmed, keep holding
            # If bullish ≥ 2, CE going ITM → exit
            if bull_score >= SWITCH_THRESHOLD:
                return ManagementDecision(
                    action=EXIT_FULL,
                    reason=f"Bullish {bull_score}/3 — CE leg going ITM. Exiting remaining call leg.",
                    score=bull_score,
                    reasons=bull_reasons,
                )

        elif legs and legs.remaining_leg == "PE":
            # Only PE (put) remains — if bullish confirmed, keep holding
            # If bearish ≥ 2, PE going ITM → exit
            if bear_score >= SWITCH_THRESHOLD:
                return ManagementDecision(
                    action=EXIT_FULL,
                    reason=f"Bearish {bear_score}/3 — PE leg going ITM. Exiting remaining put leg.",
                    score=bear_score,
                    reasons=bear_reasons,
                )

        return ManagementDecision(action=HOLD, reason="Strangle — no actionable signal change.", score=0)

    # ── Partial profit lock (CALL SELL / PUT SELL only) ─────────
    # When unrealised P&L ≥ threshold and we have more than 1 lot, exit all-but-1.
    # The last lot runs free; the 2-consecutive-signal exit will clean it up.
    if (
        trade.action in ("CALL_SELL", "PUT_SELL")
        and not trade.partial_profit_locked
        and trade.lots > NIFTY_LOT_SIZE          # must have at least 2 lots to partially exit
        and current_ltp is not None
    ):
        unrealised = (trade.entry_premium - current_ltp) * trade.lots
        if unrealised >= PARTIAL_PROFIT_LOCK_PNL:
            lock_qty = trade.lots - NIFTY_LOT_SIZE
            logger.info(
                f"Partial profit lock: unrealised ₹{unrealised:,.0f} ≥ ₹{PARTIAL_PROFIT_LOCK_PNL:,}"
                f" — exiting {lock_qty} of {trade.lots} shares, 1 lot ({NIFTY_LOT_SIZE}) running free"
            )
            return ManagementDecision(
                action=PARTIAL_PROFIT_LOCK,
                reason=(
                    f"Unrealised ₹{unrealised:,.0f} hit target — locking profit on {lock_qty} shares, "
                    f"leaving {NIFTY_LOT_SIZE} shares (1 lot) to run free"
                ),
                score=0,
            )

    # ── CALL SELL management (bearish position) ───────────────────
    if trade.action == "CALL_SELL":
        # 1. Roll check
        if not hedge_active and roll_ctx and roll_ctx.roll_allowed and roll_ctx.rolls_today < MAX_ROLLS_PER_DAY:
            call_wall_shift = ((roll_ctx.entry_call_wall or roll_ctx.call_wall) - roll_ctx.call_wall)
            breakout_ok     = roll_ctx.below_support_count >= BREAKOUT_CONFIRM_CANDLES
            if (call_wall_shift >= ROLL_THRESHOLD_PTS or breakout_ok) and bear_score >= 2:
                return ManagementDecision(
                    action=ROLL_DOWN,
                    reason=(
                        f"Roll down: call wall {roll_ctx.entry_call_wall}→{roll_ctx.call_wall} "
                        f"(-{call_wall_shift}pts)  breakout={breakout_ok}  bear={bear_score}/3"
                    ),
                    score=bear_score,
                    reasons=bear_reasons,
                )

        # 2. Exit on sustained opposing signals — protect profit before it evaporates
        if bull_score >= 2 and reversal_candle_count >= REVERSAL_CONFIRM_CANDLES:
            return ManagementDecision(
                action=EXIT_FULL,
                reason=(
                    f"Bullish {bull_score}/3 for {reversal_candle_count} consecutive candles "
                    f"— exiting CALL SELL to protect P&L"
                ),
                score=bull_score,
                reasons=bull_reasons,
            )

        # 3. Remove hedge when signals have been clean
        if hedge_active and clean_after_hedge_count >= CLEAN_CONFIRM_CANDLES:
            return ManagementDecision(
                action=REMOVE_HEDGE_LEG,
                reason=f"Signals clean for {clean_after_hedge_count} candles — removing PE hedge.",
                score=bear_score,
                reasons=bear_reasons,
            )

        return ManagementDecision(
            action=HOLD,
            reason=(
                f"CALL SELL — holding. "
                f"{'Reversal '+str(reversal_candle_count)+'/'+str(REVERSAL_CONFIRM_CANDLES) if reversal_candle_count > 0 else 'No reversal signal'}"
            ),
            score=0,
        )

    # ── PUT SELL management (bullish position) ────────────────────
    if trade.action == "PUT_SELL":
        # 1. Roll check (only when not hedged)
        if not hedge_active and roll_ctx and roll_ctx.roll_allowed and roll_ctx.rolls_today < MAX_ROLLS_PER_DAY:
            put_wall_shift = (roll_ctx.put_wall - (roll_ctx.entry_put_wall or roll_ctx.put_wall))
            breakout_ok    = roll_ctx.above_resistance_count >= BREAKOUT_CONFIRM_CANDLES
            if (put_wall_shift >= ROLL_THRESHOLD_PTS or breakout_ok) and bull_score >= 2:
                return ManagementDecision(
                    action=ROLL_UP,
                    reason=(
                        f"Roll up: put wall {roll_ctx.entry_put_wall}→{roll_ctx.put_wall} "
                        f"(+{put_wall_shift}pts)  breakout={breakout_ok}  bull={bull_score}/3"
                    ),
                    score=bull_score,
                    reasons=bull_reasons,
                )

        # 2. Exit on sustained opposing signals — protect profit before it evaporates
        if bear_score >= 2 and reversal_candle_count >= REVERSAL_CONFIRM_CANDLES:
            return ManagementDecision(
                action=EXIT_FULL,
                reason=(
                    f"Bearish {bear_score}/3 for {reversal_candle_count} consecutive candles "
                    f"— exiting PUT SELL to protect P&L"
                ),
                score=bear_score,
                reasons=bear_reasons,
            )

        # 3. Remove hedge when signals have been clean
        if hedge_active and clean_after_hedge_count >= CLEAN_CONFIRM_CANDLES:
            return ManagementDecision(
                action=REMOVE_HEDGE_LEG,
                reason=f"Signals clean for {clean_after_hedge_count} candles — removing CE hedge.",
                score=bull_score,
                reasons=bull_reasons,
            )

        return ManagementDecision(
            action=HOLD,
            reason=(
                f"PUT SELL — holding. "
                f"{'Reversal '+str(reversal_candle_count)+'/'+str(REVERSAL_CONFIRM_CANDLES) if reversal_candle_count > 0 else 'No reversal signal'}"
            ),
            score=0,
        )

    return ManagementDecision(action=HOLD, reason="No management rule matched.", score=0)
