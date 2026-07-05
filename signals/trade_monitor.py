"""
Monitors an active option-sell position for early warning signs every 15 minutes.

Checks (against current open trade):
  1. RSI divergence DEVELOPING in the opposite direction
  2. Change-in-OI building against the trade (fresh OI at adverse strikes)
  3. PCR reversing against the trade (comparing current vs previous scan)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from data.option_chain import OptionChainData
from strategy.rsi_divergence import RSIResult

logger = logging.getLogger(__name__)

# PCR shift threshold to trigger a warning (absolute change)
PCR_REVERSAL_THRESHOLD = 0.15

# COI threshold: if adverse-side COI > this fraction of total OI, flag it
COI_ADVERSE_FRACTION = 0.10


@dataclass
class TradeState:
    action:        str        # "CALL_SELL" | "PUT_SELL"
    strike:        int
    symbol:        str
    entry_time:    datetime
    entry_premium: float
    entry_spot:    float
    sl_spot_level: float
    expiry:        str
    entry_put_wall:   Optional[int]   = None   # put wall OI at trade entry (for PUT_SELL)
    entry_call_wall:  Optional[int]   = None   # call wall OI at trade entry (for CALL_SELL)
    entry_resistance: Optional[float] = None   # resistance level at trade entry
    entry_support:    Optional[float] = None   # support level at trade entry
    lots:                int             = 0      # quantity in shares (lots × lot_size)
    hedge_symbol:          Optional[str]   = None   # far-OTM hedge bought for margin reduction
    hedge_strike:          Optional[int]   = None   # strike of hedge leg (for LTP lookup at exit)
    hedge_entry_premium:   Optional[float] = None   # price paid for hedge at entry (for P&L)
    sl_put:                Optional[float] = None   # strangle put-leg SL (sl_spot_level = call-leg SL)
    partial_profit_locked: bool            = False  # True after partial exit fires (prevents re-trigger)


@dataclass
class WarningItem:
    severity:     str   # "CRITICAL" | "CAUTION"
    category:     str   # "RSI_DIVERGENCE" | "COI_REVERSAL" | "PCR_REVERSAL"
    detail:       str


@dataclass
class MonitorResult:
    has_warning:   bool
    warnings:      list[WarningItem] = field(default_factory=list)
    current_spot:  float = 0.0
    current_pcr:   float = 0.0
    current_change_pcr: float = 0.0


class TradeMonitor:
    """Stateful monitor — holds the current trade and previous scan snapshot."""

    def __init__(self):
        self.trade:      Optional[TradeState]     = None
        self.prev_pcr:   Optional[float]          = None
        self.prev_change_pcr: Optional[float]     = None
        # previous scan's per-strike COI snapshot  {strike: change_oi}
        self.prev_call_coi: dict[int, float]      = {}
        self.prev_put_coi:  dict[int, float]      = {}
        self.reversal_candle_count:    int  = 0
        self.clean_after_hedge_count:  int  = 0
        self.above_resistance_count:   int  = 0
        self.below_support_count:      int  = 0
        self.hedge_active:             bool = False
        self.rolls_today:              int  = 0

    # ── Trade lifecycle ───────────────────────────────────────────

    def set_trade(self, trade: TradeState):
        self.trade = trade
        logger.info(f"Trade monitor: tracking {trade.action} {trade.strike} @ ₹{trade.entry_premium}")

    def clear_trade(self):
        self.trade = None
        self.reversal_candle_count   = 0
        self.clean_after_hedge_count = 0
        self.above_resistance_count  = 0
        self.below_support_count     = 0
        self.hedge_active            = False
        # Do NOT reset rolls_today — persists for the session
        logger.info("Trade monitor: cleared (no active trade)")

    # ── Main check ────────────────────────────────────────────────

    def check(self, oc: OptionChainData, rsi: RSIResult, spot: float) -> MonitorResult:
        """
        Run all warning checks against the active trade.
        Also updates previous-scan snapshots for next call.
        """
        result = MonitorResult(
            has_warning=False,
            current_spot=spot,
            current_pcr=oc.pcr,
            current_change_pcr=oc.change_pcr,
        )

        if self.trade is None:
            self._update_snapshot(oc)
            return result

        warnings: list[WarningItem] = []

        if self.trade.action == "CALL_SELL":
            warnings += self._check_call_sell(oc, rsi, spot)
        elif self.trade.action == "PUT_SELL":
            warnings += self._check_put_sell(oc, rsi, spot)

        result.warnings    = warnings
        result.has_warning = len(warnings) > 0

        if warnings:
            for w in warnings:
                logger.warning(f"[{w.severity}] {w.category}: {w.detail}")

        # Track consecutive candles where RSI divergence opposes the trade direction
        if self.trade.action == "CALL_SELL":
            if rsi.bullish_divergence or rsi.bullish_divergence_developing:
                self.reversal_candle_count += 1
                logger.info(f"Reversal candle count: {self.reversal_candle_count} (bullish vs CALL_SELL)")
            else:
                self.reversal_candle_count = 0
        elif self.trade.action == "PUT_SELL":
            if rsi.bearish_divergence or rsi.bearish_divergence_developing:
                self.reversal_candle_count += 1
                logger.info(f"Reversal candle count: {self.reversal_candle_count} (bearish vs PUT_SELL)")
            else:
                self.reversal_candle_count = 0

        self._update_snapshot(oc)
        return result

    # ── CALL SELL checks (we lose if market rallies) ──────────────

    def _check_call_sell(self, oc: OptionChainData, rsi: RSIResult, spot: float) -> list[WarningItem]:
        w = []

        # 1. Bullish RSI divergence developing → market may turn UP
        if rsi.bullish_divergence or rsi.bullish_divergence_developing:
            severity = "CRITICAL" if rsi.bullish_divergence else "CAUTION"
            label    = "confirmed" if rsi.bullish_divergence else "developing"
            w.append(WarningItem(
                severity=severity,
                category="RSI_DIVERGENCE",
                detail=(
                    f"Bullish RSI divergence {label} "
                    f"(RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current}) — "
                    f"market may rally against CALL SELL at {self.trade.strike}"
                ),
            ))

        # 2. Fresh PUT OI unwinding (put writers exiting = losing bullish support)
        #    → market may fall through support we were counting on, BUT since we're
        #    selling calls this is actually neutral/helpful... skip.

        # 3. Fresh CALL OI building at strikes BELOW our sold strike = bullish pressure
        adverse_call_coi = sum(
            max(s.change_oi, 0)
            for strike, s in oc.call_data.items()
            if strike < self.trade.strike
        )
        total_call_oi = sum(s.oi for s in oc.call_data.values()) or 1
        if adverse_call_coi / total_call_oi > COI_ADVERSE_FRACTION:
            w.append(WarningItem(
                severity="CAUTION",
                category="COI_REVERSAL",
                detail=(
                    f"Fresh call OI building below {self.trade.strike} "
                    f"(COI ratio {adverse_call_coi/total_call_oi:.1%}) — "
                    f"bullish activity detected"
                ),
            ))

        # 4. PCR rising significantly (market turning bullish = against call sell)
        if self.prev_pcr is not None:
            pcr_change = oc.pcr - self.prev_pcr
            if pcr_change > PCR_REVERSAL_THRESHOLD:
                w.append(WarningItem(
                    severity="CAUTION",
                    category="PCR_REVERSAL",
                    detail=(
                        f"PCR rose {self.prev_pcr:.2f}→{oc.pcr:.2f} "
                        f"(+{pcr_change:.2f}) — put writing increasing, "
                        f"bullish sentiment building"
                    ),
                ))

        # 5. Spot approaching SL level
        if spot >= self.trade.sl_spot_level - 30:
            w.append(WarningItem(
                severity="CRITICAL",
                category="SL_PROXIMITY",
                detail=(
                    f"Nifty spot {spot:.1f} within 30pts of SL "
                    f"{self.trade.sl_spot_level:.1f} — consider exiting"
                ),
            ))

        return w

    # ── PUT SELL checks (we lose if market falls) ─────────────────

    def _check_put_sell(self, oc: OptionChainData, rsi: RSIResult, spot: float) -> list[WarningItem]:
        w = []

        # 1. Bearish RSI divergence developing → market may turn DOWN
        if rsi.bearish_divergence or rsi.bearish_divergence_developing:
            severity = "CRITICAL" if rsi.bearish_divergence else "CAUTION"
            label    = "confirmed" if rsi.bearish_divergence else "developing"
            w.append(WarningItem(
                severity=severity,
                category="RSI_DIVERGENCE",
                detail=(
                    f"Bearish RSI divergence {label} "
                    f"(RSI {rsi.rsi_prev_pivot}→{rsi.rsi_current}) — "
                    f"market may fall against PUT SELL at {self.trade.strike}"
                ),
            ))

        # 2. Fresh PUT OI building at strikes ABOVE our sold strike = bearish pressure
        adverse_put_coi = sum(
            max(s.change_oi, 0)
            for strike, s in oc.put_data.items()
            if strike > self.trade.strike
        )
        total_put_oi = sum(s.oi for s in oc.put_data.values()) or 1
        if adverse_put_coi / total_put_oi > COI_ADVERSE_FRACTION:
            w.append(WarningItem(
                severity="CAUTION",
                category="COI_REVERSAL",
                detail=(
                    f"Fresh put OI building above {self.trade.strike} "
                    f"(COI ratio {adverse_put_coi/total_put_oi:.1%}) — "
                    f"bearish activity detected"
                ),
            ))

        # 3. PCR falling significantly (market turning bearish = against put sell)
        if self.prev_pcr is not None:
            pcr_change = self.prev_pcr - oc.pcr
            if pcr_change > PCR_REVERSAL_THRESHOLD:
                w.append(WarningItem(
                    severity="CAUTION",
                    category="PCR_REVERSAL",
                    detail=(
                        f"PCR fell {self.prev_pcr:.2f}→{oc.pcr:.2f} "
                        f"(-{pcr_change:.2f}) — call writing increasing, "
                        f"bearish sentiment building"
                    ),
                ))

        # 4. Spot approaching SL level
        if spot <= self.trade.sl_spot_level + 30:
            w.append(WarningItem(
                severity="CRITICAL",
                category="SL_PROXIMITY",
                detail=(
                    f"Nifty spot {spot:.1f} within 30pts of SL "
                    f"{self.trade.sl_spot_level:.1f} — consider exiting"
                ),
            ))

        return w

    def update_breakout_count(self, spot: float, resistance: Optional[float], support: Optional[float]):
        if self.trade is None:
            return
        if self.trade.action == "PUT_SELL":
            if resistance and spot > resistance:
                self.above_resistance_count += 1
            else:
                self.above_resistance_count = 0
        elif self.trade.action == "CALL_SELL":
            if support and spot < support:
                self.below_support_count += 1
            else:
                self.below_support_count = 0

    def _update_snapshot(self, oc: OptionChainData):
        self.prev_pcr        = oc.pcr
        self.prev_change_pcr = oc.change_pcr
        self.prev_call_coi   = {s: d.change_oi for s, d in oc.call_data.items()}
        self.prev_put_coi    = {s: d.change_oi for s, d in oc.put_data.items()}
