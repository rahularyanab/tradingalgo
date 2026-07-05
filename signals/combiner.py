"""
Combines the three signal modules into a final trading decision.

Directional:
  Score 3/3 → STRONG  → 3 lots
  Score 2/3 → MODERATE → 2 lots
  Score 1/3 → WEAK    → suppressed

Non-directional (Strangle):
  PCR neutral + RSI neutral + both S/R visible + no strong directional bias
  → STRANGLE: sell CALL at resistance + PUT at support, 2 lots each leg
"""

from dataclasses import dataclass, field
from typing import Optional

from config import (
    MIN_SIGNAL_SCORE, SL_BUFFER_POINTS, TARGET_DECAY_PCT,
    PCR_NEUTRAL_LOW, PCR_NEUTRAL_HIGH,
    RSI_NEUTRAL_LOW, RSI_NEUTRAL_HIGH,
    LOTS_STRONG, LOTS_MODERATE, LOTS_STRANGLE,
    PCR_BEARISH, PCR_BULLISH,
)
from strategy.trendline import TrendlineResult
from strategy.rsi_divergence import RSIResult
from strategy.option_signal import OptionSignal


STRENGTH_LABEL = {3: "STRONG", 2: "MODERATE", 1: "WEAK", 0: "NONE"}


@dataclass
class FinalSignal:
    action:        str       # "CALL_SELL" | "PUT_SELL" | "STRANGLE" | "NO_SIGNAL"
    strength:      str       # "STRONG" | "MODERATE" | "STRANGLE" | "WEAK"
    score:         int       # 0-3
    lots:          int       # lots for directional leg
    # ── Directional fields ────────────────────────────────────────
    strike:        Optional[int]   = None
    symbol:        Optional[str]   = None
    premium:       Optional[float] = None
    sl_spot_level: Optional[float] = None
    trendline_level: Optional[float] = None
    # ── Strangle fields ───────────────────────────────────────────
    call_strike:   Optional[int]   = None
    put_strike:    Optional[int]   = None
    call_symbol:   Optional[str]   = None
    put_symbol:    Optional[str]   = None
    call_premium:  Optional[float] = None
    put_premium:   Optional[float] = None
    call_sl:       Optional[float] = None
    put_sl:        Optional[float] = None
    strangle_lots: int             = LOTS_STRANGLE
    # ── Hedge legs (far OTM buys ~₹5-6 for margin reduction) ─────
    hedge_call_strike: Optional[int]   = None
    hedge_call_symbol: Optional[str]   = None
    hedge_call_ltp:    Optional[float] = None
    hedge_put_strike:  Optional[int]   = None
    hedge_put_symbol:  Optional[str]   = None
    hedge_put_ltp:     Optional[float] = None
    net_premium:       Optional[float] = None   # sold - bought (after hedge)
    # ── Common fields ─────────────────────────────────────────────
    expiry:        Optional[str]   = None
    spot_price:    float           = 0.0
    target_decay:  float           = TARGET_DECAY_PCT
    rsi_current:   float           = 0.0
    rsi_prev:      float           = 0.0
    price_prev_pivot: float        = 0.0
    pcr:           float           = 0.0
    change_pcr:    float           = 0.0
    call_wall:     int             = 0
    put_wall:      int             = 0
    max_pain:      int             = 0
    top_2_calls:   list            = field(default_factory=list)
    top_2_puts:    list            = field(default_factory=list)
    reasons:       list            = field(default_factory=list)
    # ── Per-component flags (for signal dashboard in Telegram) ────
    bearish_divergence:   bool          = False
    bullish_divergence:   bool          = False
    at_resistance:        bool          = False
    at_support:           bool          = False
    call_writing_bearish: bool          = False
    put_writing_bullish:  bool          = False
    call_bearish_pcr:     bool          = False   # PCR < 0.8
    put_bullish_pcr:      bool          = False   # PCR > 1.2
    resistance_level:     Optional[float] = None
    support_level:        Optional[float] = None


def _is_non_directional(
    tl: TrendlineResult,
    rsi: RSIResult,
    opt: OptionSignal,
    call_score: int,
    put_score: int,
) -> bool:
    """
    Non-directional when:
    - Neither call nor put score reaches threshold
    - RSI in neutral zone (40–60)
    - PCR in neutral zone (0.8–1.2)
    - Both support AND resistance trendlines visible
    - Enough range between them (≥ 0.5%) to make strangle worthwhile
    """
    if call_score >= MIN_SIGNAL_SCORE or put_score >= MIN_SIGNAL_SCORE:
        return False

    rsi_neutral = RSI_NEUTRAL_LOW <= rsi.rsi_current <= RSI_NEUTRAL_HIGH
    pcr_neutral = PCR_NEUTRAL_LOW <= opt.pcr <= PCR_NEUTRAL_HIGH
    both_levels = tl.resistance_level is not None and tl.support_level is not None

    if not (rsi_neutral and pcr_neutral and both_levels):
        return False

    range_pct = (tl.resistance_level - tl.support_level) / tl.support_level
    return range_pct >= 0.005   # at least 0.5% range


def combine_signals(
    tl:  TrendlineResult,
    rsi: RSIResult,
    opt: OptionSignal,
    spot_price: float,
    expiry: str,
) -> FinalSignal:

    # Hedge fields — passed through from OptionSignal
    _hedge = dict(
        hedge_call_strike=opt.hedge_call_strike,
        hedge_call_symbol=opt.hedge_call_symbol,
        hedge_call_ltp=opt.hedge_call_ltp,
        hedge_put_strike=opt.hedge_put_strike,
        hedge_put_symbol=opt.hedge_put_symbol,
        hedge_put_ltp=opt.hedge_put_ltp,
    )

    # Signal component flags — used by Telegram dashboard
    _flags = dict(
        bearish_divergence=rsi.bearish_divergence,
        bullish_divergence=rsi.bullish_divergence,
        at_resistance=tl.at_resistance,
        at_support=tl.at_support,
        call_writing_bearish=opt.call_writing_bearish,
        put_writing_bullish=opt.put_writing_bullish,
        call_bearish_pcr=opt.call_bearish,
        put_bullish_pcr=opt.put_bullish,
        resistance_level=tl.resistance_level,
        support_level=tl.support_level,
    )

    _common = dict(
        expiry=expiry,
        spot_price=spot_price,
        target_decay=TARGET_DECAY_PCT,
        rsi_current=rsi.rsi_current,
        rsi_prev=rsi.rsi_prev_pivot,
        price_prev_pivot=rsi.price_prev_pivot,
        pcr=opt.pcr,
        change_pcr=opt.change_pcr,
        call_wall=opt.call_wall,
        put_wall=opt.put_wall,
        max_pain=opt.max_pain,
        top_2_calls=opt.top_2_calls,
        top_2_puts=opt.top_2_puts,
    )

    # ── Score directional signals ─────────────────────────────────
    call_score, call_reasons = 0, []
    if tl.at_resistance:
        call_score += 1
        call_reasons.append(f"Trendline resistance at {tl.resistance_level:.1f}")
    if rsi.bearish_divergence:
        call_score += 1
        call_reasons.append(
            f"Bearish RSI divergence (RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current}  "
            f"price {rsi.price_prev_pivot:.0f}→{rsi.price_current:.0f})"
        )
    if opt.call_bearish:
        call_score += 1
        call_reasons.append(f"PCR {opt.pcr} (bearish <0.8)  |  call wall {opt.call_wall}")
    elif opt.call_writing_bearish:
        # Fresh call writing at near-ATM strikes — counts as the OI leg even when PCR is neutral
        call_score += 1
        call_reasons.append(f"Call writing building at {opt.call_wall} & nearby  |  PCR {opt.pcr}")

    put_score, put_reasons = 0, []
    if tl.at_support:
        put_score += 1
        put_reasons.append(f"Trendline support at {tl.support_level:.1f}")
    if rsi.bullish_divergence:
        put_score += 1
        put_reasons.append(
            f"Bullish RSI divergence (RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current}  "
            f"price {rsi.price_prev_pivot:.0f}→{rsi.price_current:.0f})"
        )
    if opt.put_bullish:
        put_score += 1
        put_reasons.append(f"PCR {opt.pcr} (bullish >1.2)  |  put wall {opt.put_wall}")
    elif opt.put_writing_bullish and opt.pcr >= PCR_BEARISH:
        # Put writing only counts when PCR is not actively bearish (<0.8).
        # If PCR is bearish, fresh put writing is likely defensive hedging, not a bullish signal.
        put_score += 1
        put_reasons.append(f"Put writing building at {opt.put_wall} & nearby  |  PCR {opt.pcr}")

    # ── CALL SELL (directional) → Bear Call Spread ───────────────
    if call_score >= MIN_SIGNAL_SCORE and call_score >= put_score:
        lots = LOTS_STRONG if call_score == 3 else LOTS_MODERATE
        sl   = (tl.resistance_level or spot_price) + SL_BUFFER_POINTS
        net  = round((opt.call_ltp or 0) - (opt.hedge_call_ltp or 0), 2)
        return FinalSignal(
            action="CALL_SELL",
            strength=STRENGTH_LABEL[call_score],
            score=call_score, lots=lots,
            strike=opt.best_call_strike,
            symbol=opt.call_symbol,
            premium=opt.call_ltp,
            net_premium=net,
            sl_spot_level=sl,
            trendline_level=tl.resistance_level,
            reasons=call_reasons,
            **_hedge, **_common, **_flags,
        )

    # ── PUT SELL (directional) → Bull Put Spread ──────────────────
    if put_score >= MIN_SIGNAL_SCORE:
        lots = LOTS_STRONG if put_score == 3 else LOTS_MODERATE
        sl   = (tl.support_level or spot_price) - SL_BUFFER_POINTS
        net  = round((opt.put_ltp or 0) - (opt.hedge_put_ltp or 0), 2)
        return FinalSignal(
            action="PUT_SELL",
            strength=STRENGTH_LABEL[put_score],
            score=put_score, lots=lots,
            strike=opt.best_put_strike,
            symbol=opt.put_symbol,
            premium=opt.put_ltp,
            net_premium=net,
            sl_spot_level=sl,
            trendline_level=tl.support_level,
            reasons=put_reasons,
            **_hedge, **_common, **_flags,
        )

    # ── STRANGLE (non-directional) ────────────────────────────────
    if _is_non_directional(tl, rsi, opt, call_score, put_score):
        call_sl = (tl.resistance_level or spot_price) + SL_BUFFER_POINTS
        put_sl  = (tl.support_level  or spot_price) - SL_BUFFER_POINTS
        reasons = [
            f"RSI neutral at {rsi.rsi_current:.1f} (range 40–60)",
            f"PCR neutral at {opt.pcr} (range 0.8–1.2)",
            f"Range: {tl.support_level:.0f} → {tl.resistance_level:.0f}",
        ]
        # Net premium for iron condor = (CE sold - CE hedge) + (PE sold - PE hedge)
        net = round(
            ((opt.call_ltp or 0) - (opt.hedge_call_ltp or 0)) +
            ((opt.put_ltp  or 0) - (opt.hedge_put_ltp  or 0)),
            2,
        )
        return FinalSignal(
            action="STRANGLE",
            strength="STRANGLE",
            score=max(call_score, put_score),
            lots=LOTS_STRANGLE,
            strangle_lots=LOTS_STRANGLE,
            call_strike=opt.best_call_strike,
            put_strike=opt.best_put_strike,
            call_symbol=opt.call_symbol,
            put_symbol=opt.put_symbol,
            call_premium=opt.call_ltp,
            put_premium=opt.put_ltp,
            net_premium=net,
            call_sl=call_sl,
            put_sl=put_sl,
            reasons=reasons,
            **_hedge, **_common, **_flags,
        )

    # ── No signal ─────────────────────────────────────────────────
    return FinalSignal(
        action="NO_SIGNAL",
        strength=STRENGTH_LABEL.get(max(call_score, put_score), "NONE"),
        score=max(call_score, put_score),
        lots=0,
        reasons=[],
        **_hedge, **_common, **_flags,
    )
