"""
Active position management — evaluates every 15-min candle while in a trade.

Rules:
  STRANGLE
    Target hit                          → exit everything
    Profit-booking ladder (both legs)   → book PROFIT_BOOK_STEP_LOTS at ₹5000, again at ₹7500
    Bullish ≥2/3                        → exit CE leg (going ITM), let PE decay to zero
    Bearish ≥2/3                        → exit PE leg (going ITM), let CE decay to zero
    Still range-bound + in profit,
    sustained PYRAMID_CONFIRM_CANDLES   → add PYRAMID_STEP_LOTS to both legs (cap MAX_LOTS_STRANGLE_LEG)

  CALL SELL (bearish position)
    Trailing profit lock                → protect gains once they've meaningfully retraced
    Profit-booking ladder                → book lots at ₹5000 / ₹7500, rest runs to EOD
    In profit + sustained bearish
    confirmation (≥MIN_SIGNAL_SCORE)     → pyramid up (cap MAX_LOTS_DIRECTIONAL)
    Opposing bullish 3/3                 → exit + enter PUT SELL (ride the reversal)
    Opposing bullish 2/3                 → exit + enter STRANGLE (trend unclear)

  PUT SELL (bullish position)
    Mirror of CALL SELL.

  Developing divergence alone → WARNING only, no exit yet

Pyramiding never applies at entry — only on top of an already-profitable
position, so the risk on fresh capital never changes from the starting size.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (PCR_BULLISH, PCR_BEARISH, MIN_SIGNAL_SCORE,
                    ROLL_THRESHOLD_PTS, MAX_ROLLS_PER_DAY,
                    BREAKOUT_CONFIRM_CANDLES, REVERSAL_CONFIRM_CANDLES, CLEAN_CONFIRM_CANDLES,
                    NIFTY_LOT_SIZE, TARGET_DECAY_PCT,
                    PROFIT_LOCK_ARM_PNL, PROFIT_LOCK_GIVEBACK_PCT,
                    PROFIT_BOOK_STEP1_PNL, PROFIT_BOOK_STEP2_PNL, PROFIT_BOOK_STEP_LOTS,
                    MAX_LOTS_DIRECTIONAL, MAX_LOTS_STRANGLE_LEG,
                    PYRAMID_CONFIRM_CANDLES, PYRAMID_STEP_LOTS)
from strategy.trendline import TrendlineResult
from strategy.rsi_divergence import RSIResult
from strategy.option_signal import OptionSignal
from signals.trade_monitor import TradeState

logger = logging.getLogger(__name__)

REVERSAL_THRESHOLD   = 3   # score needed to fully reverse into opposite directional trade
SWITCH_THRESHOLD     = 3   # raised from 2 → 3: 2/3 opposing signals now just HOLDs,
                           # avoids excessive CALL↔PUT churn when market is ranging
PYRAMID_MIN_SCORE    = 3   # STRONG only — pyramiding must clear a higher bar than a fresh
                           # MODERATE entry (Aug 20: 2/3 was enough to triple size on ₹130 of "profit")


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
    # Far-OTM margin hedges, one per side (bought alongside the sold leg)
    hedge_ce_strike:        Optional[int]   = None
    hedge_ce_symbol:        Optional[str]   = None
    hedge_ce_entry_premium: Optional[float] = None
    hedge_pe_strike:        Optional[int]   = None
    hedge_pe_symbol:        Optional[str]   = None
    hedge_pe_entry_premium: Optional[float] = None

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
    lots:       Optional[int]  = None   # lot count for PROFIT_BOOK (book) / PYRAMID_ADD (add)


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
PROFIT_BOOK          = "PROFIT_BOOK"          # book `lots` lots of profit at a ladder checkpoint
PYRAMID_ADD          = "PYRAMID_ADD"          # add `lots` lots on top of an already-profitable position


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


def _book_lots(current_lot_count: int) -> int:
    """Lots to book at a ladder rung — clamped so at least 1 lot keeps running."""
    return min(PROFIT_BOOK_STEP_LOTS, current_lot_count - 1)


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
    strangle_ce_ltp:         Optional[float] = None,
    strangle_pe_ltp:         Optional[float] = None,
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

        # Target check first — a profitable target takes priority over
        # adjusting a single leg. Same formula as the paper-trading engine:
        # combined entry premium vs. current premium of whichever legs are
        # still active (a leg already closed keeps counting toward the
        # original combined baseline, same as paper trading does).
        active_prem_sum: Optional[float] = None
        if legs:
            current_prems = []
            if legs.ce_active and strangle_ce_ltp is not None:
                current_prems.append(strangle_ce_ltp)
            if legs.pe_active and strangle_pe_ltp is not None:
                current_prems.append(strangle_pe_ltp)
            active_count = (1 if legs.ce_active else 0) + (1 if legs.pe_active else 0)
            if current_prems and len(current_prems) == active_count:
                active_prem_sum = sum(current_prems)
                target_prem = trade.entry_premium * (1 - TARGET_DECAY_PCT)
                if active_prem_sum <= target_prem:
                    return ManagementDecision(
                        action=EXIT_FULL,
                        reason=(
                            f"Target hit: combined premium ₹{active_prem_sum:.2f} "
                            f"≤ ₹{target_prem:.2f} ({int(TARGET_DECAY_PCT*100)}% decay from ₹{trade.entry_premium:.2f})"
                        ),
                        score=0,
                    )

        current_lot_count = trade.lots // NIFTY_LOT_SIZE
        unrealised_strangle = (
            (trade.entry_premium - active_prem_sum) * trade.lots
            if active_prem_sum is not None else None
        )

        # ── Profit-booking ladder (both legs together) ──────────────
        if (
            legs and legs.ce_active and legs.pe_active
            and unrealised_strangle is not None
            and current_lot_count > 1
        ):
            if trade.profit_booked_step1 and not trade.profit_booked_step2 \
                    and unrealised_strangle >= PROFIT_BOOK_STEP2_PNL:
                book_lots = _book_lots(current_lot_count)
                if book_lots > 0:
                    return ManagementDecision(
                        action=PROFIT_BOOK, lots=book_lots,
                        reason=(
                            f"Unrealised ₹{unrealised_strangle:,.0f} hit step-2 (₹{PROFIT_BOOK_STEP2_PNL:,}) "
                            f"— booking {book_lots} lot(s) each leg, rest runs to EOD"
                        ),
                        score=0,
                    )
            elif not trade.profit_booked_step1 and unrealised_strangle >= PROFIT_BOOK_STEP1_PNL:
                book_lots = _book_lots(current_lot_count)
                if book_lots > 0:
                    return ManagementDecision(
                        action=PROFIT_BOOK, lots=book_lots,
                        reason=(
                            f"Unrealised ₹{unrealised_strangle:,.0f} hit step-1 (₹{PROFIT_BOOK_STEP1_PNL:,}) "
                            f"— booking {book_lots} lot(s) each leg"
                        ),
                        score=0,
                    )

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

            # ── Pyramid add: still genuinely range-bound AND a real cushion ──
            # Requires unrealised >= PROFIT_LOCK_ARM_PNL, not just > 0 — a
            # trivial paper profit isn't evidence the position has earned
            # more risk (Aug 20: pyramided a directional trade on ₹130/₹663
            # of "profit" and tripled the size right before it reversed).
            still_neutral = bull_score < SWITCH_THRESHOLD and bear_score < SWITCH_THRESHOLD
            if (
                unrealised_strangle is not None and unrealised_strangle >= PROFIT_LOCK_ARM_PNL
                and not trade.pyramid_disabled
                and still_neutral
                and current_lot_count < MAX_LOTS_STRANGLE_LEG
            ):
                trade.pyramid_confirm_count += 1
                if trade.pyramid_confirm_count >= PYRAMID_CONFIRM_CANDLES:
                    add_lots = min(PYRAMID_STEP_LOTS, MAX_LOTS_STRANGLE_LEG - current_lot_count)
                    return ManagementDecision(
                        action=PYRAMID_ADD, lots=add_lots,
                        reason=(
                            f"Range-bound + cushioned (₹{unrealised_strangle:,.0f} ≥ ₹{PROFIT_LOCK_ARM_PNL:,}) for "
                            f"{trade.pyramid_confirm_count} candles — adding {add_lots} lot(s) to both legs "
                            f"(cap {MAX_LOTS_STRANGLE_LEG})"
                        ),
                        score=0,
                    )
            else:
                trade.pyramid_confirm_count = 0

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

    # ── Track peak unrealised profit (at entry lot size) ───────────
    # Tracked at entry_lots rather than the live (booked-down/pyramided-up)
    # trade.lots, so a lot-count change doesn't distort the high-water mark.
    if trade.action in ("CALL_SELL", "PUT_SELL") and current_ltp is not None:
        tracked_unrealised = (trade.entry_premium - current_ltp) * trade.entry_lots
        if tracked_unrealised > trade.peak_unrealised:
            trade.peak_unrealised = tracked_unrealised

    # ── Trailing profit lock — protect gains once they've meaningfully retraced ──
    # Arms once unrealised has ever reached PROFIT_LOCK_ARM_PNL; once armed, a
    # retrace of PROFIT_LOCK_GIVEBACK_PCT from that peak exits immediately. This
    # catches a trade that was comfortably in profit but never crossed the
    # profit-booking ladder below, then round-tripped into a loss.
    if (
        trade.action in ("CALL_SELL", "PUT_SELL")
        and current_ltp is not None
        and trade.peak_unrealised >= PROFIT_LOCK_ARM_PNL
    ):
        current_unrealised_tracked = (trade.entry_premium - current_ltp) * trade.entry_lots
        giveback_floor = trade.peak_unrealised * (1 - PROFIT_LOCK_GIVEBACK_PCT)
        if current_unrealised_tracked <= giveback_floor:
            return ManagementDecision(
                action=EXIT_FULL,
                reason=(
                    f"Trailing profit lock: unrealised ₹{current_unrealised_tracked:,.0f} retraced from peak "
                    f"₹{trade.peak_unrealised:,.0f} (floor ₹{giveback_floor:,.0f}, "
                    f"{int(PROFIT_LOCK_GIVEBACK_PCT*100)}% giveback) — exiting to protect gains"
                ),
                score=0,
            )

    # ── Profit-booking ladder (CALL SELL / PUT SELL) ───────────────
    # Uses the REAL current position (current lots, not entry_lots) — this is
    # about banking real money, unlike the entry_lots-normalised trailing lock above.
    if trade.action in ("CALL_SELL", "PUT_SELL") and current_ltp is not None:
        current_lot_count = trade.lots // NIFTY_LOT_SIZE
        unrealised_now = (trade.entry_premium - current_ltp) * trade.lots
        if current_lot_count > 1:
            if trade.profit_booked_step1 and not trade.profit_booked_step2 \
                    and unrealised_now >= PROFIT_BOOK_STEP2_PNL:
                book_lots = _book_lots(current_lot_count)
                if book_lots > 0:
                    return ManagementDecision(
                        action=PROFIT_BOOK, lots=book_lots,
                        reason=(
                            f"Unrealised ₹{unrealised_now:,.0f} hit step-2 (₹{PROFIT_BOOK_STEP2_PNL:,}) "
                            f"— booking {book_lots} lot(s), rest runs to EOD"
                        ),
                        score=0,
                    )
            elif not trade.profit_booked_step1 and unrealised_now >= PROFIT_BOOK_STEP1_PNL:
                book_lots = _book_lots(current_lot_count)
                if book_lots > 0:
                    return ManagementDecision(
                        action=PROFIT_BOOK, lots=book_lots,
                        reason=(
                            f"Unrealised ₹{unrealised_now:,.0f} hit step-1 (₹{PROFIT_BOOK_STEP1_PNL:,}) "
                            f"— booking {book_lots} lot(s)"
                        ),
                        score=0,
                    )

        # ── Pyramid add: real cushion + STRONG sustained confirmation ──
        # For CALL_SELL, bear_score confirms (bearish = favourable); for PUT_SELL, bull_score.
        # Gated on peak_unrealised (entry_lots basis, same bar that arms the trailing
        # lock) rather than unrealised_now > 0 — a trivial paper profit isn't evidence
        # the position has earned more risk, and peak_unrealised stays measured against
        # the ORIGINAL starting size so a second add can't get progressively cheaper as
        # lots grow. Also raised the score bar to STRONG (3/3): Aug 20 pyramided a
        # directional trade on ₹130 then ₹663 of "profit" with just a 2/3 signal — the
        # same bar as a fresh entry — and tripled the size right before it reversed.
        favourable_score = bear_score if trade.action == "CALL_SELL" else bull_score
        if (
            not trade.pyramid_disabled
            and trade.peak_unrealised >= PROFIT_LOCK_ARM_PNL
            and favourable_score >= PYRAMID_MIN_SCORE
            and current_lot_count < MAX_LOTS_DIRECTIONAL
        ):
            trade.pyramid_confirm_count += 1
            if trade.pyramid_confirm_count >= PYRAMID_CONFIRM_CANDLES:
                add_lots = min(PYRAMID_STEP_LOTS, MAX_LOTS_DIRECTIONAL - current_lot_count)
                return ManagementDecision(
                    action=PYRAMID_ADD, lots=add_lots,
                    reason=(
                        f"Cushioned (peak ₹{trade.peak_unrealised:,.0f} ≥ ₹{PROFIT_LOCK_ARM_PNL:,}) with STRONG "
                        f"{favourable_score}/3 confirming for {trade.pyramid_confirm_count} candles — "
                        f"adding {add_lots} lot(s) (cap {MAX_LOTS_DIRECTIONAL})"
                    ),
                    score=favourable_score,
                )
        else:
            trade.pyramid_confirm_count = 0

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
