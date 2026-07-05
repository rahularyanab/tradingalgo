"""
Backtest engine — Intraday Option Selling.

Signals: Trendline S/R + RSI Divergence (2/3 for directional, neutral for strangle).
Option pricing: Black-Scholes.

Trade management:
  - Entry      : 09:30–14:45 only (no new trades after 14:45)
  - Force exit : 15:00 sharp (all open positions closed)
  - SL         : spot crosses trendline ± SL_BUFFER_POINTS
  - Target     : premium decays by TARGET_DECAY_PCT
  - Directional STRONG  → 3 lots | MODERATE → 2 lots
  - Strangle            → 2 lots each leg
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd

from backtest.option_pricer import OptionType, price_option_at_spot
from config import (
    STRIKE_STEP,
    SL_BUFFER_POINTS,
    TARGET_DECAY_PCT,
    NIFTY_LOT_SIZE,
    PIVOT_LOOKBACK,
    STRANGLE_CUTOFF_HOUR, STRANGLE_CUTOFF_MIN,
    FRIDAY_STRANGLE_CUTOFF, STRANGLE_SL_BUFFER,
    TRENDLINE_PIVOTS,
    PROXIMITY_PCT,
    RSI_PERIOD,
    LOTS_STRONG, LOTS_MODERATE, LOTS_STRANGLE,
    PCR_NEUTRAL_LOW, PCR_NEUTRAL_HIGH,
    RSI_NEUTRAL_LOW, RSI_NEUTRAL_HIGH,
)
from strategy.trendline import analyse_trendlines
from strategy.rsi_divergence import analyse_rsi_divergence
from signals.position_manager import (
    evaluate_position, StrangleLegState,
    EXIT_CE_LEG, EXIT_PE_LEG, EXIT_FULL, HOLD,
    REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE,
)

logger = logging.getLogger(__name__)

WARMUP_BARS   = 60
MIN_PREMIUM   = 30.0
MIN_BARS_BETWEEN_TRADES = 4
ENTRY_CUTOFF_HOUR = 14     # no new entries at or after 14:45
ENTRY_CUTOFF_MIN  = 45
FORCE_EXIT_HOUR   = 15
FORCE_EXIT_MIN    = 0


@dataclass
class TradeEvent:
    time:   datetime
    action: str      # "ENTRY"|"HOLD"|"EXIT_CE"|"EXIT_PE"|"REVERSE"|"SWITCH"|"EXIT"
    reason: str
    score:  int
    spot:   float


@dataclass
class BacktestTrade:
    trade_id:        int
    entry_time:      datetime
    action:          str      # "CALL_SELL" | "PUT_SELL" | "STRANGLE"
    strike:          int      # primary strike (call for strangle)
    expiry:          date
    entry_spot:      float
    entry_premium:   float
    sl_spot:         float
    target_premium:  float
    lots:            int                 = 1
    put_strike:      Optional[int]       = None
    put_sl:          Optional[float]     = None
    exit_time:       Optional[datetime]  = None
    exit_spot:       Optional[float]     = None
    exit_premium:    Optional[float]     = None
    exit_reason:     Optional[str]       = None
    pnl_per_lot:     Optional[float]     = None
    pnl_total:       Optional[float]     = None
    # Position manager tracking
    events:          list = field(default_factory=list)   # list[TradeEvent]
    ce_active:       bool  = True    # strangle: is CE leg still open?
    pe_active:       bool  = True    # strangle: is PE leg still open?
    ce_closed_pnl:   float = 0.0    # P&L realised when CE leg closed early
    pe_closed_pnl:   float = 0.0    # P&L realised when PE leg closed early


EXPIRY_WEEKDAY = 1   # Tuesday (0=Mon,1=Tue,2=Wed,3=Thu,4=Fri)


def _next_expiry(from_date: date, skip_today: bool = False) -> date:
    """
    Returns next Nifty weekly expiry (Tuesday).
    skip_today=True: if today IS expiry day, return next week's expiry instead.
    """
    days = (EXPIRY_WEEKDAY - from_date.weekday()) % 7
    if days == 0:
        days = 7 if skip_today else 0
    if days == 0:
        days = 7      # safety: always go forward at least to next occurrence
    return from_date + timedelta(days=days)


def _days_to_expiry(current_dt, expiry: date) -> float:
    # Strip timezone so arithmetic works regardless of data source
    if hasattr(current_dt, "tzinfo") and current_dt.tzinfo is not None:
        current_dt = current_dt.replace(tzinfo=None)
    expiry_dt = datetime.combine(expiry, datetime.strptime("15:25", "%H:%M").time())
    return max((expiry_dt - current_dt).total_seconds() / 86400, 0.01)


def _select_strike(spot: float, action: str, trendline_level: float, sigma: float,
                   days_to_exp: float) -> tuple[Optional[int], Optional[float]]:
    """
    Mimic live strike selection: skip nearest, go to next S/R level.
    Walk back toward ATM if premium < MIN_PREMIUM.
    Returns (strike, estimated_premium) or (None, None) if no suitable strike.
    """
    opt_type = OptionType.CALL if action == "CALL_SELL" else OptionType.PUT

    # Nearest strike based on trendline level
    nearest = round(trendline_level / STRIKE_STEP) * STRIKE_STEP

    if action == "CALL_SELL":
        # Start 2 strikes above nearest resistance
        candidate = nearest + 2 * STRIKE_STEP
        floor     = nearest      # don't go below nearest resistance
        step      = -STRIKE_STEP  # walk toward ATM if too cheap
    else:
        # Start 2 strikes below nearest support
        candidate = nearest - 2 * STRIKE_STEP
        ceiling   = nearest      # don't go above nearest support
        step      = +STRIKE_STEP  # walk toward ATM if too cheap

    for _ in range(5):
        prem = price_option_at_spot(spot, candidate, days_to_exp, sigma, opt_type)
        if prem >= MIN_PREMIUM:
            return candidate, prem
        candidate += step
        # Guard against crossing ATM
        if action == "CALL_SELL" and candidate < floor:
            break
        if action == "PUT_SELL" and candidate > ceiling:
            break

    return None, None


class BacktestEngine:
    def __init__(
        self,
        df:                   pd.DataFrame,
        sigma:                float = 0.14,
        lot_size:             int   = NIFTY_LOT_SIZE,
        sl_buffer:            int   = SL_BUFFER_POINTS,
        target_decay:         float = TARGET_DECAY_PCT,
        use_position_manager: bool  = True,
    ):
        self.df                   = df
        self.sigma                = sigma
        self.lot_size             = lot_size
        self.sl_buffer            = sl_buffer
        self.target_decay         = target_decay
        self.use_position_manager = use_position_manager

        self.trades:          list[BacktestTrade] = []
        self.current_trade:   Optional[BacktestTrade] = None
        self._trade_counter   = 0
        self._bars_since_exit = MIN_BARS_BETWEEN_TRADES
        self._window_cache:   Optional[pd.DataFrame] = None  # last computed window

    def run(self) -> list[BacktestTrade]:
        logger.info(
            f"Backtest: {len(self.df)} candles  "
            f"sigma={self.sigma}  lot={self.lot_size}  sl_buf={self.sl_buffer}"
        )

        for i in range(WARMUP_BARS, len(self.df)):
            window = self.df.iloc[: i + 1]
            candle = self.df.iloc[i]
            dt     = pd.Timestamp(candle.name).tz_localize(None).to_pydatetime()
            self._window_cache = window   # used by position manager in _check_exit

            if self.current_trade:
                self._check_exit(dt, candle)

            if not self.current_trade and self._bars_since_exit >= MIN_BARS_BETWEEN_TRADES:
                self._check_entry(window, candle, dt)
            else:
                if not self.current_trade:
                    self._bars_since_exit += 1

        # Force-close any open trade at last candle
        if self.current_trade:
            self._force_close(self.df.iloc[-1])

        logger.info(f"Backtest complete. {len(self.trades)} trades executed.")
        return self.trades

    # ── Entry logic ───────────────────────────────────────────────

    def _intraday_entry_ok(self, dt: datetime) -> bool:
        """Allow entries 09:30–14:45 only."""
        t = dt.hour * 60 + dt.minute
        return (ENTRY_CUTOFF_HOUR * 60 - 15) >= t >= (9 * 60 + 30)

    def _is_force_exit_time(self, dt: datetime) -> bool:
        return dt.hour * 60 + dt.minute >= FORCE_EXIT_HOUR * 60 + FORCE_EXIT_MIN

    def _check_entry(self, window: pd.DataFrame, candle, dt: datetime):
        if not self._intraday_entry_ok(dt):
            return
        try:
            tl  = analyse_trendlines(window)
            rsi = analyse_rsi_divergence(window)
        except Exception:
            return

        spot          = float(candle["close"])
        is_expiry_day = (dt.weekday() == EXPIRY_WEEKDAY)
        # On expiry day: no new entries (use next week's expiry per user rule)
        if is_expiry_day:
            return
        expiry      = _next_expiry(dt.date(), skip_today=True)
        days_to_exp = _days_to_expiry(dt, expiry)

        # ── Directional ───────────────────────────────────────────
        action = tl_level = sl_spot = None
        lots   = 1

        if tl.at_resistance and rsi.bearish_divergence and tl.resistance_level:
            action   = "CALL_SELL"
            tl_level = tl.resistance_level
            sl_spot  = tl.resistance_level + self.sl_buffer
            # Score: resistance(1) + div(1) = 2 → MODERATE; both present = STRONG counted as 3
            lots = LOTS_STRONG   # both trendline + RSI confirm = treat as strong

        elif tl.at_support and rsi.bullish_divergence and tl.support_level:
            action   = "PUT_SELL"
            tl_level = tl.support_level
            sl_spot  = tl.support_level - self.sl_buffer
            lots = LOTS_STRONG

        if action:
            strike, premium = _select_strike(spot, action, tl_level, self.sigma, days_to_exp)
            if strike is None or premium is None:
                return
            self._trade_counter += 1
            trade = BacktestTrade(
                trade_id=self._trade_counter, entry_time=dt,
                action=action, strike=strike, expiry=expiry,
                entry_spot=spot, entry_premium=premium,
                sl_spot=sl_spot, lots=lots,
                target_premium=round(premium * (1 - self.target_decay), 2),
            )
            self.current_trade = trade
            logger.info(
                f"ENTRY #{self._trade_counter}  {action}  {strike}  "
                f"lots={lots}  spot={spot:.1f}  prem=₹{premium}  "
                f"sl={sl_spot:.1f}  [{dt}]"
            )
            return

        # ── Strangle (non-directional) ────────────────────────────────
        # No strangles on Friday after 12 PM (weekend gap risk)
        if dt.weekday() == 4 and dt.hour >= FRIDAY_STRANGLE_CUTOFF:
            return
        strangle_cutoff = STRANGLE_CUTOFF_HOUR * 60 + STRANGLE_CUTOFF_MIN
        if dt.hour * 60 + dt.minute >= strangle_cutoff:
            return

        rsi_neutral = RSI_NEUTRAL_LOW <= rsi.rsi_current <= RSI_NEUTRAL_HIGH
        both_levels = tl.resistance_level is not None and tl.support_level is not None
        if not (rsi_neutral and both_levels):
            return
        range_pct = (tl.resistance_level - tl.support_level) / tl.support_level
        if range_pct < 0.005:
            return

        c_strike, c_prem = _select_strike(spot, "CALL_SELL", tl.resistance_level, self.sigma, days_to_exp)
        p_strike, p_prem = _select_strike(spot, "PUT_SELL",  tl.support_level,    self.sigma, days_to_exp)
        if not (c_strike and p_strike and c_prem and p_prem):
            return

        combined_prem = round(c_prem + p_prem, 2)
        self._trade_counter += 1
        trade = BacktestTrade(
            trade_id=self._trade_counter, entry_time=dt,
            action="STRANGLE", strike=c_strike,
            put_strike=p_strike, expiry=expiry,
            entry_spot=spot, entry_premium=combined_prem,
            sl_spot=tl.resistance_level + STRANGLE_SL_BUFFER,   # wider: 80 pts
            put_sl=tl.support_level   - STRANGLE_SL_BUFFER,     # wider: 80 pts
            lots=LOTS_STRANGLE,
            target_premium=round(combined_prem * (1 - self.target_decay), 2),
        )
        self.current_trade = trade
        logger.info(
            f"ENTRY #{self._trade_counter}  STRANGLE  "
            f"CE={c_strike}@₹{c_prem}  PE={p_strike}@₹{p_prem}  "
            f"lots={LOTS_STRANGLE}  [{dt}]"
        )

    # ── Exit logic ────────────────────────────────────────────────

    def _check_exit(self, dt: datetime, candle):
        trade       = self.current_trade
        spot        = float(candle["close"])
        days_to_exp = _days_to_expiry(dt, trade.expiry)

        # ── Force exit at 15:00 — directional trades only ────────────
        if self._is_force_exit_time(dt) and trade.action != "STRANGLE":
            trade.events.append(TradeEvent(dt, "EXIT", "Force exit 3PM", 0, spot))
            self._close_trade(dt, spot, days_to_exp, "FORCE_EXIT")
            return

        # ── Position manager (runs before SL check) ───────────────
        if self.use_position_manager and self._window_cache is not None:
            self._run_position_manager(dt, spot, days_to_exp, trade)
            if self.current_trade is None:
                return   # trade was closed by PM
            trade = self.current_trade   # may have been replaced (reversal)

        # ── Strangle exit (overnight hold) ───────────────────────────
        if trade.action == "STRANGLE":
            # Compute P&L only for active legs
            c_prem = (price_option_at_spot(spot, trade.strike,    days_to_exp, self.sigma, OptionType.CALL)
                      if trade.ce_active else 0)
            p_prem = (price_option_at_spot(spot, trade.put_strike, days_to_exp, self.sigma, OptionType.PUT)
                      if trade.pe_active else 0)
            combined = round(c_prem + p_prem, 2)

            active_entry = (
                (trade.entry_premium if trade.ce_active else 0) +
                (trade.entry_premium if trade.pe_active else 0)
            )
            # SL check — only for active legs
            if trade.ce_active and spot >= trade.sl_spot:
                trade.events.append(TradeEvent(dt, "EXIT", f"SL CE hit spot {spot:.0f}", 0, spot))
                self._close_trade(dt, spot, days_to_exp, "SL_CE", combined)
            elif trade.pe_active and trade.put_sl and spot <= trade.put_sl:
                trade.events.append(TradeEvent(dt, "EXIT", f"SL PE hit spot {spot:.0f}", 0, spot))
                self._close_trade(dt, spot, days_to_exp, "SL_PE", combined)
            elif combined <= trade.target_premium:
                trade.events.append(TradeEvent(dt, "EXIT", "Target 65% decay hit", 0, spot))
                self._close_trade(dt, spot, days_to_exp, "TARGET", combined)
            elif dt.date() >= trade.expiry and dt.hour >= 15 and dt.minute >= 20:
                c_intr = max(spot - trade.strike,     0) if trade.ce_active else 0
                p_intr = max(trade.put_strike - spot, 0) if trade.pe_active else 0
                trade.events.append(TradeEvent(dt, "EXIT", "Expiry", 0, spot))
                self._close_trade(dt, spot, days_to_exp, "EXPIRY", round(c_intr + p_intr, 2))
            return

        # ── Directional exit ──────────────────────────────────────
        opt_type     = OptionType.CALL if trade.action == "CALL_SELL" else OptionType.PUT
        current_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, opt_type)

        exit_reason  = None
        exit_premium = current_prem

        if trade.action == "CALL_SELL" and spot >= trade.sl_spot:
            exit_reason  = "SL"
            exit_premium = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, opt_type)

        elif trade.action == "PUT_SELL" and spot <= trade.sl_spot:
            exit_reason  = "SL"
            exit_premium = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, opt_type)

        # Target: premium decayed enough
        elif current_prem <= trade.target_premium:
            exit_reason  = "TARGET"
            exit_premium = current_prem

        # Expiry: Tuesday at/after 15:25
        elif dt.date() >= trade.expiry and dt.hour >= 15 and dt.minute >= 25:
            exit_reason  = "EXPIRY"
            if opt_type == OptionType.CALL:
                exit_premium = max(spot - trade.strike, 0)
            else:
                exit_premium = max(trade.strike - spot, 0)

        if exit_reason:
            self._close_trade(dt, spot, days_to_exp, exit_reason, exit_premium)

    # ── Position manager integration ──────────────────────────────

    def _run_position_manager(self, dt, spot, days_to_exp, trade):
        """Run position manager signals and apply any management decisions."""
        window = self._window_cache
        try:
            tl  = analyse_trendlines(window)
            rsi = analyse_rsi_divergence(window)
        except Exception:
            return

        # Neutral opt_signal for backtest (no live OI — only trendline+RSI drive PM)
        from strategy.option_signal import OptionSignal
        from data.option_chain import StrikeData
        neutral_opt = OptionSignal(
            call_bearish=False, put_bullish=False,
            best_call_strike=None, best_put_strike=None,
            call_symbol=None, put_symbol=None,
            call_ltp=None, put_ltp=None,
            pcr=1.0, change_pcr=1.0,
            call_wall=0, put_wall=0, max_pain=0,
            top_2_calls=[], top_2_puts=[],
        )

        # Build strangle leg state if needed
        sl_state = None
        if trade.action == "STRANGLE":
            sl_state = StrangleLegState(
                ce_strike=trade.strike,
                ce_symbol=f"CE{trade.strike}",
                ce_entry_premium=trade.entry_premium / 2,
                pe_strike=trade.put_strike or 0,
                pe_symbol=f"PE{trade.put_strike}",
                pe_entry_premium=trade.entry_premium / 2,
                ce_active=trade.ce_active,
                pe_active=trade.pe_active,
            )

        # Trade needs .action attribute — BacktestTrade already has it
        decision = evaluate_position(trade, tl, rsi, neutral_opt, sl_state)

        if decision.action == HOLD:
            return

        # Log the management event
        trade.events.append(TradeEvent(
            time=dt, action=decision.action,
            reason=decision.reason, score=decision.score, spot=spot,
        ))
        logger.info(
            f"  PM #{trade.trade_id}  {decision.action}  score={decision.score}  "
            f"spot={spot:.0f}  [{dt}]"
        )

        if decision.action == EXIT_CE_LEG and trade.ce_active:
            c_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, OptionType.CALL)
            trade.ce_closed_pnl = round((trade.entry_premium / 2 - c_prem) * trade.lots * self.lot_size, 2)
            trade.ce_active = False
            if sl_state:
                sl_state.ce_active = False
            logger.info(f"  CE leg closed @ ₹{c_prem:.2f}  pnl=₹{trade.ce_closed_pnl:,.0f}")

        elif decision.action == EXIT_PE_LEG and trade.pe_active:
            p_prem = price_option_at_spot(spot, trade.put_strike, days_to_exp, self.sigma, OptionType.PUT)
            trade.pe_closed_pnl = round((trade.entry_premium / 2 - p_prem) * trade.lots * self.lot_size, 2)
            trade.pe_active = False
            if sl_state:
                sl_state.pe_active = False
            logger.info(f"  PE leg closed @ ₹{p_prem:.2f}  pnl=₹{trade.pe_closed_pnl:,.0f}")

        elif decision.action in (EXIT_FULL, REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE):
            # Close everything, then optionally enter new trade
            c_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, OptionType.CALL) if trade.ce_active else 0
            p_prem = (price_option_at_spot(spot, trade.put_strike, days_to_exp, self.sigma, OptionType.PUT)
                      if (trade.action == "STRANGLE" and trade.pe_active) else 0)
            combined = round(c_prem + p_prem, 2)

            # Account for already-closed legs
            already_closed_pnl = trade.ce_closed_pnl + trade.pe_closed_pnl
            self._close_trade(dt, spot, days_to_exp, f"MANAGED:{decision.action}", combined,
                              extra_pnl=already_closed_pnl)

            # Re-enter if reversal/switch and entry is still allowed
            if decision.new_action and self._intraday_entry_ok(dt):
                self._try_reentry(dt, spot, decision.new_action, tl, days_to_exp)

    def _try_reentry(self, dt, spot, new_action, tl, days_to_exp):
        """Attempt to enter a new position after a management-triggered exit."""
        expiry = _next_expiry(dt.date(), skip_today=True)

        if new_action == "PUT_SELL" and tl.support_level:
            strike, prem = _select_strike(spot, "PUT_SELL", tl.support_level, self.sigma, days_to_exp)
            if strike and prem:
                self._trade_counter += 1
                new_trade = BacktestTrade(
                    trade_id=self._trade_counter, entry_time=dt,
                    action="PUT_SELL", strike=strike, expiry=expiry,
                    entry_spot=spot, entry_premium=prem,
                    sl_spot=tl.support_level - self.sl_buffer,
                    lots=LOTS_MODERATE,
                    target_premium=round(prem * (1 - self.target_decay), 2),
                )
                new_trade.events.append(TradeEvent(dt, "ENTRY", "Post-reversal entry", 0, spot))
                self.current_trade = new_trade
                logger.info(f"  REENTRY PUT_SELL {strike} @ ₹{prem}  [{dt}]")

        elif new_action == "CALL_SELL" and tl.resistance_level:
            strike, prem = _select_strike(spot, "CALL_SELL", tl.resistance_level, self.sigma, days_to_exp)
            if strike and prem:
                self._trade_counter += 1
                new_trade = BacktestTrade(
                    trade_id=self._trade_counter, entry_time=dt,
                    action="CALL_SELL", strike=strike, expiry=expiry,
                    entry_spot=spot, entry_premium=prem,
                    sl_spot=tl.resistance_level + self.sl_buffer,
                    lots=LOTS_MODERATE,
                    target_premium=round(prem * (1 - self.target_decay), 2),
                )
                new_trade.events.append(TradeEvent(dt, "ENTRY", "Post-reversal entry", 0, spot))
                self.current_trade = new_trade
                logger.info(f"  REENTRY CALL_SELL {strike} @ ₹{prem}  [{dt}]")

    def _close_trade(self, dt, spot, days_to_exp, reason, exit_prem=None, extra_pnl=0.0):
        trade = self.current_trade
        if exit_prem is None:
            if trade.action == "STRANGLE":
                # Price both legs — this was the critical bug previously
                c_prem    = price_option_at_spot(spot, trade.strike,    days_to_exp, self.sigma, OptionType.CALL)
                p_prem    = price_option_at_spot(spot, trade.put_strike, days_to_exp, self.sigma, OptionType.PUT)
                exit_prem = round(c_prem + p_prem, 2)
            elif trade.action == "CALL_SELL":
                exit_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, OptionType.CALL)
            else:
                exit_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, OptionType.PUT)

        pnl_per_lot = round(trade.entry_premium - exit_prem, 2)
        pnl_total   = round(pnl_per_lot * trade.lots * self.lot_size + extra_pnl, 2)

        trade.exit_time    = dt
        trade.exit_spot    = spot
        trade.exit_premium = exit_prem
        trade.exit_reason  = reason
        trade.pnl_per_lot  = pnl_per_lot
        trade.pnl_total    = pnl_total

        self.trades.append(trade)
        self.current_trade    = None
        self._bars_since_exit = 0

        logger.info(
            f"EXIT  #{trade.trade_id}  {trade.action}  {trade.strike}  "
            f"lots={trade.lots}  reason={reason}  pnl=₹{pnl_total}  [{dt}]"
        )

    def _force_close(self, candle):
        trade       = self.current_trade
        dt          = pd.Timestamp(candle.name).tz_localize(None).to_pydatetime()
        spot        = float(candle["close"])
        days_to_exp = _days_to_expiry(dt, trade.expiry)
        if trade.action == "STRANGLE":
            c_prem    = price_option_at_spot(spot, trade.strike,    days_to_exp, self.sigma, OptionType.CALL)
            p_prem    = price_option_at_spot(spot, trade.put_strike, days_to_exp, self.sigma, OptionType.PUT)
            exit_prem = round(c_prem + p_prem, 2)
        else:
            opt_type  = OptionType.CALL if trade.action == "CALL_SELL" else OptionType.PUT
            exit_prem = price_option_at_spot(spot, trade.strike, days_to_exp, self.sigma, opt_type)

        pnl_per_lot = round(trade.entry_premium - exit_prem, 2)
        trade.exit_time    = dt
        trade.exit_spot    = spot
        trade.exit_premium = exit_prem
        trade.exit_reason  = "END_OF_DATA"
        trade.pnl_per_lot  = pnl_per_lot
        trade.pnl_total    = round(pnl_per_lot * self.lot_size, 2)
        self.trades.append(trade)
        self.current_trade = None
