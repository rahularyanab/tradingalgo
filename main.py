"""
Nifty Intraday + Overnight Option Selling Bot
=============================================
Run:  python main.py

Trade management (every 15-min candle):
  STRANGLE:
    Bullish ≥2/3 → exit CE leg, hold PE
    Bearish ≥2/3 → exit PE leg, hold CE
  CALL SELL:
    Bullish 3/3  → exit + enter PUT SELL
    Bullish 2/3  → exit + enter STRANGLE
  PUT SELL:
    Bearish 3/3  → exit + enter CALL SELL
    Bearish 2/3  → exit + enter STRANGLE

Positions can pyramid up (in profit only, never on fresh capital) toward
MAX_LOTS_DIRECTIONAL / MAX_LOTS_STRANGLE_LEG, and book profit in two steps
along the way (₹5000 → 2 lots, ₹7500 → 2 more). A daily-loss hard stop at
-₹DAILY_MAX_LOSS force-closes the open position, not just new entries.

Everything — directionals and strangles — is flattened in one shot at
EOD_FLATTEN (config.py; currently 3:25 PM) every day: purely intraday.
No new strangles on Friday after 12 PM.

Requires:
1. .env with credentials
2. python auth/kite_login.py  (once each morning before 9:15)
"""

import csv
import json
import logging
import math
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

import schedule

import paths as _paths
import control as _control
_paths.ensure_dirs()          # per-instance logs/ and data/ (no-op for the default instance)

from auth.kite_login import load_access_token
from execution.order_manager import (
    place_sell_order, place_buy_order, place_spread_entry, place_buy_limit_exact, modify_order_price,
)
from config import (
    MARKET_OPEN_HOUR, MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN,
    ENTRY_START_HOUR, ENTRY_START_MIN,
    FORCE_EXIT_HOUR, FORCE_EXIT_MIN,
    EOD_FLATTEN_HOUR, EOD_FLATTEN_MIN, EOD_FLATTEN_RETRY_AFTER_MIN, EOD_VERIFY_AFTER_MIN,
    EXIT_VERIFY_DELAY_SEC, EXIT_VERIFY_REALERT_MIN, EXIT_VERIFY_GIVE_UP_MIN,
    EXIT_CHASE_ENABLED, EXIT_CHASE_BUFFER_PCT, EXIT_CHASE_MIN_BUFFER,
    STRANGLE_CUTOFF_HOUR, STRANGLE_CUTOFF_MIN,
    FRIDAY_STRANGLE_CUTOFF, STRANGLE_SL_BUFFER,
    NIFTY_LOT_SIZE,
    DAILY_MAX_LOSS, REENTRY_COOLDOWN_CANDLES, MAX_SAME_DIR_REENTRIES,
    MAX_LOTS_DIRECTIONAL, MAX_LOTS_STRANGLE_LEG, LOW_PREMIUM_THRESHOLD,
    DAY_PROFIT_ARM_PNL, DAY_PROFIT_FLOOR, PROTECTED_MAX_LOTS, PROTECTED_DELTA_EST,
    GUARD_INTERVAL_SECONDS, SETTINGS,
    LATE_ENTRY_HOUR, LATE_ENTRY_MIN, LATE_ENTRY_SIZE_FACTOR,
)
from data.market_data import get_kite_client, fetch_nifty_candles, get_current_nifty_price
from data.option_chain import fetch_option_chain
from notifications.telegram_bot import (
    send_signal, send_trade_warning, send_management_alert,
    send_live_pnl_update, send_error_alert, send_trade_journal_entry, _post,
)
from notifications.telegram_commands import start_command_listener
from signals.combiner import combine_signals
from signals.position_manager import (
    evaluate_position, StrangleLegState, ManagementDecision,
    EXIT_CE_LEG, EXIT_PE_LEG, EXIT_FULL,
    REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE, HOLD,
    PROFIT_BOOK, PYRAMID_ADD,
)
from signals.trade_monitor import TradeMonitor, TradeState
from strategy.option_signal import analyse_option_signal
from strategy.rsi_divergence import analyse_rsi_divergence
from strategy.trendline import analyse_trendlines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_paths.BOT_LOG),
    ],
)
logger = logging.getLogger("main")

from execution.pnl_report import JOURNAL_PATH

JOURNAL_FIELDS = [
    "date", "exit_time", "action", "strike", "expiry",
    "entry_time", "entry_spot", "exit_spot",
    "entry_premium", "exit_premium",
    "hedge_strike", "hedge_entry_premium", "hedge_exit_premium",
    "lots", "main_pnl", "hedge_pnl", "net_pnl", "exit_reason",
]


_EOD_LABEL = f"{EOD_FLATTEN_HOUR - 12}:{EOD_FLATTEN_MIN:02d} PM"   # derived from config so the text can't drift


def _eod_verify_times() -> list:
    """['15:26', '15:29'] — an early "did it fill?" check and a final one a
    minute before the close."""
    first = EOD_FLATTEN_HOUR * 60 + EOD_FLATTEN_MIN + EOD_VERIFY_AFTER_MIN
    last  = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN - 1
    times = sorted({min(first, last), last})
    return [f"{t // 60:02d}:{t % 60:02d}" for t in times]


def _eod_flatten_times() -> list:
    """['15:25', '15:27'] — the flatten and its one retry. The retry is capped
    inside the session (market closes MARKET_CLOSE_HOUR:MIN)."""
    first = EOD_FLATTEN_HOUR * 60 + EOD_FLATTEN_MIN
    retry = min(first + EOD_FLATTEN_RETRY_AFTER_MIN, MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN - 1)
    times = [first] + ([retry] if retry > first else [])
    return [f"{t // 60:02d}:{t % 60:02d}" for t in times]


def _fmt_pnl(p: float) -> str:
    return f"+₹{p:,.0f}" if p >= 0 else f"−₹{abs(p):,.0f}"


def _current_unrealised(trade: TradeState, sl, current_ltp, strangle_ce_ltp, strangle_pe_ltp):
    """Unrealised P&L (₹) of the currently open position at its REAL current
    size — main leg(s) only, no hedge netting (same convention as the
    profit-booking ladder / trailing lock). None if an LTP is unavailable.
    `sl` is the active StrangleLegState (or None)."""
    if trade.action == "STRANGLE":
        if sl is None:
            return None
        total = 0.0
        if sl.ce_active:
            if strangle_ce_ltp is None:
                return None
            total += (sl.ce_entry_premium - strangle_ce_ltp) * trade.lots
        if sl.pe_active:
            if strangle_pe_ltp is None:
                return None
            total += (sl.pe_entry_premium - strangle_pe_ltp) * trade.lots
        return total
    if current_ltp is None:
        return None
    return (trade.entry_premium - current_ltp) * trade.lots


def _fetch_oc_for_trade(kite, trade_expiry: str, oc_nearest=None):
    """
    Return the option chain matching `trade_expiry` — an open trade may be on
    a different expiry than "current week" if it was entered via the
    low-premium next-week fallback (_apply_low_premium_fallback). Reuses
    `oc_nearest` if it already matches (avoids a redundant fetch); otherwise
    fetches nearest week fresh, then next week.

    Returns None if neither matches — callers MUST skip management for this
    cycle rather than fall back to a mismatched chain: reading the wrong
    week's chain at the same strike number returns a completely different
    contract's premium, which is what silently corrupted P&L, the
    profit-booking ladder, pyramiding, and worst of all priced a real exit
    order off the wrong contract (Aug 24 incident — the order never filled).
    """
    oc = oc_nearest if oc_nearest is not None else fetch_option_chain(kite=kite)
    if oc and oc.weekly_expiry_date == trade_expiry:
        return oc
    alt_oc = fetch_option_chain(kite=kite, next_week=True)
    if alt_oc and alt_oc.weekly_expiry_date == trade_expiry:
        return alt_oc
    logger.error(
        f"Could not fetch option chain matching expiry {trade_expiry} "
        f"(nearest={oc.weekly_expiry_date if oc else 'fetch failed'}, "
        f"next-week={alt_oc.weekly_expiry_date if alt_oc else 'fetch failed'})"
    )
    return None


def _apply_low_premium_fallback(kite, final, tl_result, rsi_result, spot):
    """
    In a low-VIX regime the near-week OTM premium can be too thin for the
    gamma risk. If the candidate entry's premium is below LOW_PREMIUM_THRESHOLD,
    try next week's expiry first (more time value at the same OTM distance =
    higher premium, LOWER gamma, without moving the strike closer to spot).
    Only switch if next week's OWN option-derived signals independently
    confirm the same trade; otherwise stay on this week's cheap signal but
    return pyramid_disabled=True, so a thin position can't scale up into an
    Aug-20-style loss.
    Returns (final_signal_to_use, pyramid_disabled).
    """
    if final.action not in ("CALL_SELL", "PUT_SELL", "STRANGLE"):
        return final, False

    if final.action == "STRANGLE":
        thin = (final.call_premium or 0) < LOW_PREMIUM_THRESHOLD or (final.put_premium or 0) < LOW_PREMIUM_THRESHOLD
    else:
        thin = (final.premium or 0) < LOW_PREMIUM_THRESHOLD
    if not thin:
        return final, False

    try:
        oc_next = fetch_option_chain(kite=kite, next_week=True)
    except Exception as e:
        oc_next = None
        logger.warning(f"Next-week option chain fetch failed: {e}")

    if not oc_next:
        logger.warning(
            "Next-week option chain unavailable — staying on this week's signal, "
            "pyramiding disabled for this trade"
        )
        return final, True

    opt_next = analyse_option_signal(
        oc=oc_next, trendline_resistance=tl_result.resistance_level,
        trendline_support=tl_result.support_level,
    )
    final_next = combine_signals(
        tl=tl_result, rsi=rsi_result, opt=opt_next,
        spot_price=spot, expiry=oc_next.weekly_expiry_date,
    )
    if final_next.action == final.action:
        logger.info(
            f"Low premium (< ₹{LOW_PREMIUM_THRESHOLD}) — next week confirms "
            f"{final_next.action}, switching to expiry {oc_next.weekly_expiry_date}"
        )
        return final_next, False

    logger.info(
        f"Low premium (< ₹{LOW_PREMIUM_THRESHOLD}) — next week does NOT confirm "
        f"(would be {final_next.action}/{final_next.score}) — staying on this week's "
        f"{final.action}, pyramiding disabled for this trade"
    )
    return final, True


def _record_trade_exit(
    trade: "TradeState",
    exit_ltp: Optional[float],
    hedge_exit_ltp: Optional[float],
    main_pnl: float,
    hedge_pnl: float,
    total_pnl: float,
    spot: float,
    exit_reason: str,
    exit_detail: Optional[str] = None,
):
    """Append trade to CSV journal and send Telegram record."""
    # Accumulate today's realised P&L for the DAILY_MAX_LOSS breaker. The day
    # rollover reset lives in run_scan(), so here we only add.
    global _day_realised_pnl
    _day_realised_pnl += total_pnl
    _update_day_lock()

    now = datetime.now()
    row = {
        "date":                 now.strftime("%Y-%m-%d"),
        "exit_time":            now.strftime("%H:%M"),
        "action":               trade.action,
        "strike":               trade.strike,
        "expiry":               trade.expiry,
        "entry_time":           trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
        "entry_spot":           f"{trade.entry_spot:.0f}" if trade.entry_spot > 0 else "",
        "exit_spot":            f"{spot:.0f}",
        "entry_premium":        f"{trade.entry_premium:.2f}",
        "exit_premium":         f"{exit_ltp:.2f}" if exit_ltp else "",
        "hedge_strike":         trade.hedge_strike or "",
        "hedge_entry_premium":  f"{trade.hedge_entry_premium:.2f}" if trade.hedge_entry_premium else "",
        "hedge_exit_premium":   f"{hedge_exit_ltp:.2f}" if hedge_exit_ltp else "",
        "lots":                 trade.lots,
        "main_pnl":             f"{main_pnl:.0f}",
        "hedge_pnl":            f"{hedge_pnl:.0f}",
        "net_pnl":              f"{total_pnl:.0f}",
        "exit_reason":          exit_reason,
    }
    try:
        write_header = not JOURNAL_PATH.exists()
        with open(JOURNAL_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        logger.info(f"Trade logged: net P&L ₹{total_pnl:.0f}  →  {JOURNAL_PATH}")
    except Exception as e:
        logger.error(f"Journal write failed: {e}")

    send_trade_journal_entry(trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, total_pnl, spot, exit_reason,
                              exit_detail=exit_detail)

    # A stop was sent to the broker — "accepted" is not "filled". Have the next
    # fill check confirm the sold leg is really gone (verify_exit_fills).
    if exit_reason in _STOP_EXIT_TAGS:
        _register_exit_verify(exit_reason, trade)


def _buy_price(oc, opt_type: str, strike: int) -> Optional[float]:
    """Current LTP + ₹2 buffer for a buy-to-cover order (rounded up to tick)."""
    data = oc.call_data if opt_type == "CE" else oc.put_data
    sd = data.get(strike)
    if sd and sd.ltp > 0:
        return math.ceil((sd.ltp + 2) / 0.05) * 0.05
    return None


def _sell_price(oc, opt_type: str, strike: int) -> Optional[float]:
    """Current LTP - ₹1 buffer for a sell order (rounded down to tick)."""
    data = oc.call_data if opt_type == "CE" else oc.put_data
    sd = data.get(strike)
    if sd and sd.ltp > 0:
        return max(math.floor((sd.ltp - 1) / 0.05) * 0.05, 0.05)
    return None


_kite         = None
_kite_token   = None
monitor       = TradeMonitor()
strangle_legs: StrangleLegState | None = None   # tracks individual strangle legs
_signal_exit_blocked: Optional[str]   = None    # direction under re-entry restriction after a SIGNAL_EXIT
_signal_exit_reset_date = None   # last date _signal_exit_blocked was reset — detects day rollover
_signal_exit_cooldown: int            = 0       # candles remaining before a same-direction re-entry is considered
_reentry_count: dict                  = {"CALL_SELL": 0, "PUT_SELL": 0}  # STRONG re-entries used today, per direction
_day_realised_pnl: float              = 0.0     # cumulative realised P&L today — drives the DAILY_MAX_LOSS breaker
_day_pnl_reset_date = None       # last date _day_realised_pnl was reset — detects day rollover
_day_loss_alerted: bool = False  # True once the daily-loss breaker has alerted for the day (avoids spamming)
_day_profit_locked: bool = False  # True once today's realised P&L has reached DAY_PROFIT_ARM_PNL (latched for the day)
_ext_close_strikes: dict = {}    # trade key -> consecutive checks that found it flat at the broker (manual close)
_guard_alerted: set = set()      # one-shot alert keys for the intra-scan guard (avoids a message per minute)
_eod_verify: list = []         # legs the EOD flatten just tried to close — checked against the broker by verify_eod_flatten()
_exit_verify: list = []        # stop-type exits (SL / day floor) awaiting a broker fill check — see verify_exit_fills()
_STOP_EXIT_TAGS = {"SL_HIT", "DAILY_LOSS_BREAKER", "DAY_PROFIT_LOCK"}   # full exits that must be verified as filled


_pause_reminded_date = None


def _paused_now() -> bool:
    """True while this bot (or every bot, via the owner) is paused: no new entries, no added size, no re-entry after a reversal.
    Never consulted by anything protective (stops, exits, EOD flatten, fill checks)."""
    return _control.is_paused()[0]


def _pause_reminder():
    """Once per day while paused, say so — a pause is easy to forget overnight."""
    global _pause_reminded_date
    paused, why = _control.is_paused()
    today = datetime.now().date()
    if not paused or _pause_reminded_date == today:
        return
    _pause_reminded_date = today
    _post(f"⏸ *Bot is PAUSED* — {why}.\nNo new trades or added size today. {_control.open_trade_note()}\nSend /resume when you want it trading again.")


_broker_ok_checked = False


def _announce_broker_ok():
    """Per-user instances: the first scan that got market data through the broker confirms the connection on Telegram."""
    global _broker_ok_checked
    if _broker_ok_checked or SETTINGS is None:
        return
    _broker_ok_checked = True
    try:
        from notifications import onboarding
        onboarding.send_broker_verified_once(SETTINGS)
    except Exception as e:
        logger.warning(f"Broker-verified notice failed: {e}")


def _get_kite():
    global _kite, _kite_token
    import broker as _broker
    token = _broker.load_token()
    if _kite is None or token != _kite_token:
        _kite = _broker.make_client(token)
        _kite_token = token
    return _kite


def _is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_mins  = MARKET_OPEN_HOUR  * 60 + MARKET_OPEN_MIN
    close_mins = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    return open_mins <= (now.hour * 60 + now.minute) <= close_mins


def _entry_allowed() -> bool:
    now      = datetime.now()
    now_mins = now.hour * 60 + now.minute
    return (ENTRY_START_HOUR * 60 + ENTRY_START_MIN) <= now_mins < (FORCE_EXIT_HOUR * 60 + FORCE_EXIT_MIN)


def _strangle_entry_allowed() -> bool:
    now = datetime.now()
    if now.weekday() == 1:                         # Tuesday = expiry day
        return False
    if now.weekday() == 4 and now.hour >= FRIDAY_STRANGLE_CUTOFF:
        return False                                # no new strangles Friday PM
    now_mins = now.hour * 60 + now.minute
    return now_mins < STRANGLE_CUTOFF_HOUR * 60 + STRANGLE_CUTOFF_MIN


# ── Day-profit protection ──────────────────────────────────────────────────────

def _day_floor() -> float:
    """Lowest day P&L (realised + unrealised) the algo tolerates before it
    force-closes and stops entering. Normally -DAILY_MAX_LOSS; once protected
    mode is armed it is DAY_PROFIT_FLOOR (0 = never let a banked profit turn
    into a loss)."""
    return DAY_PROFIT_FLOOR if _day_profit_locked else -DAILY_MAX_LOSS


def _update_day_lock():
    """Arm protected mode the moment today's realised P&L reaches
    DAY_PROFIT_ARM_PNL. Latches for the rest of the day."""
    global _day_profit_locked
    if _day_profit_locked or _day_realised_pnl < DAY_PROFIT_ARM_PNL:
        return
    _day_profit_locked = True
    if monitor.trade is not None:
        monitor.trade.pyramid_disabled = True      # no scaling up an already-open trade either
    logger.info(
        f"PROFIT LOCK armed: realised ₹{_day_realised_pnl:,.0f} >= ₹{DAY_PROFIT_ARM_PNL:,} — "
        f"max {PROTECTED_MAX_LOTS} lot(s) per trade, no pyramiding, day floor ₹{DAY_PROFIT_FLOOR:,}"
    )
    _post(
        f"🔒 *PROFIT LOCK ARMED*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Booked today: *{_fmt_pnl(_day_realised_pnl)}*  (arm level ₹{DAY_PROFIT_ARM_PNL:,})\n"
        f"For the rest of the day:\n"
        f"• new trades capped at *{PROTECTED_MAX_LOTS} lots* (1 if 2 won't fit under the floor)\n"
        f"• no pyramiding\n"
        f"• everything closes if today's P&L falls to *₹{DAY_PROFIT_FLOOR:,}* — the stop-loss is the profit already banked"
    )
    _save_state()


def _protected_lots(requested_lots: int, sl_distance_pts: float) -> int:
    """Lots allowed for a NEW trade. Normal mode: unchanged. Protected mode:
    at most PROTECTED_MAX_LOTS, and reduced (down to 1) until the estimated
    loss at the SL fits inside today's banked profit — so one stop-out
    cannot take the day below the floor."""
    if not _day_profit_locked:
        return requested_lots
    lots   = max(1, min(requested_lots, PROTECTED_MAX_LOTS))
    cushion = _day_realised_pnl - DAY_PROFIT_FLOOR
    loss_per_lot = max(sl_distance_pts, 1.0) * PROTECTED_DELTA_EST * NIFTY_LOT_SIZE
    while lots > 1 and lots * loss_per_lot > cushion:
        lots -= 1
    return lots


def _apply_late_entry_sizing(final, now: Optional[datetime] = None):
    """New entries from LATE_ENTRY_HOUR:MIN onward are sized down by
    LATE_ENTRY_SIZE_FACTOR (rounded down, minimum 1 lot). Mutates `final` so
    the Telegram signal shows the size actually traded."""
    now = now or datetime.now()
    if final.action not in ("CALL_SELL", "PUT_SELL", "STRANGLE"):
        return final
    if now.hour * 60 + now.minute < LATE_ENTRY_HOUR * 60 + LATE_ENTRY_MIN:
        return final
    attr = "strangle_lots" if final.action == "STRANGLE" else "lots"
    cur  = getattr(final, attr)
    new  = max(1, int(cur * LATE_ENTRY_SIZE_FACTOR))
    if new < cur:
        setattr(final, attr, new)
        note = f"⏰ Late entry (after {LATE_ENTRY_HOUR:02d}:{LATE_ENTRY_MIN:02d}): sized {new} lot(s) instead of {cur}"
        final.reasons.append(note)
        logger.info(note)
    return final


def _apply_profit_lock_sizing(final, spot: float, pyramid_disabled: bool):
    """Shrink the signal's lot count in protected mode (mutates `final` so the
    Telegram signal message shows the size that is really being traded) and
    switch pyramiding off for the new trade. No-op in normal mode."""
    if not _day_profit_locked or final.action not in ("CALL_SELL", "PUT_SELL", "STRANGLE"):
        return final, pyramid_disabled

    if final.action == "CALL_SELL":
        dist = (final.sl_spot_level or spot) - spot
    elif final.action == "PUT_SELL":
        dist = spot - (final.sl_spot_level or spot)
    else:
        dist = max((final.call_sl or spot) - spot, spot - (final.put_sl or spot))

    attr = "strangle_lots" if final.action == "STRANGLE" else "lots"
    cur  = getattr(final, attr)
    new  = _protected_lots(cur, dist)
    if new != cur:
        setattr(final, attr, new)
        note = f"🔒 Profit-lock mode: sized {new} lot(s) instead of {cur} (day banked ₹{_day_realised_pnl:,.0f})"
        final.reasons.append(note)
        logger.info(note)
    return final, True


def _floor_breach_decision(total_today: float, unrealised_now: float) -> ManagementDecision:
    """EXIT_FULL decision for 'day P&L hit its floor' — the daily-loss breaker
    in normal mode, the profit-lock floor in protected mode."""
    if _day_profit_locked:
        reason = (
            f"🔒 Day profit-lock: today's P&L ₹{total_today:,.0f} (banked ₹{_day_realised_pnl:,.0f} "
            f"+ open ₹{unrealised_now:,.0f}) fell to the ₹{DAY_PROFIT_FLOOR:,} floor — "
            f"closing so the day doesn't turn negative"
        )
    else:
        reason = (
            f"🛑 Daily loss breaker: today's P&L ₹{total_today:,.0f} (realised "
            f"₹{_day_realised_pnl:,.0f} + unrealised ₹{unrealised_now:,.0f}) breached "
            f"-₹{DAILY_MAX_LOSS:,} — force-closing to protect capital"
        )
    return ManagementDecision(action=EXIT_FULL, reason=reason, reasons=[reason], score=0)


_STATE_FILE = _paths.STATE_FILE


def _save_state():
    """
    Persist exactly what THIS bot placed to disk. This is the only source of
    truth used to restore state on restart — never re-derived by guessing
    from whatever happens to be open in the Kite account, since that can't
    be told apart from a position placed manually in the same account.
    """
    trade = monitor.trade
    state = {
        "date": datetime.now().strftime("%Y-%m-%d"), "trade": None, "strangle_legs": None,
        # Day-level risk state — without this a mid-day restart (deploys!) would forget
        # what was already banked/lost today and silently disarm the profit lock.
        "day_realised_pnl": _day_realised_pnl, "day_profit_locked": _day_profit_locked,
    }

    if trade:
        state["trade"] = {
            "action":                trade.action,
            "strike":                trade.strike,
            "symbol":                trade.symbol,
            "entry_time":            trade.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_premium":         trade.entry_premium,
            "entry_spot":            trade.entry_spot,
            "sl_spot_level":         trade.sl_spot_level,
            "expiry":                trade.expiry,
            "entry_put_wall":        trade.entry_put_wall,
            "entry_call_wall":       trade.entry_call_wall,
            "entry_resistance":      trade.entry_resistance,
            "entry_support":         trade.entry_support,
            "lots":                  trade.lots,
            "hedge_symbol":          trade.hedge_symbol,
            "hedge_strike":          trade.hedge_strike,
            "hedge_entry_premium":   trade.hedge_entry_premium,
            "sl_put":                trade.sl_put,
            "peak_unrealised":       trade.peak_unrealised,
            "entry_lots":            trade.entry_lots,
            "profit_booked_step1":   trade.profit_booked_step1,
            "profit_booked_step2":   trade.profit_booked_step2,
            "pyramid_confirm_count": trade.pyramid_confirm_count,
            "pyramid_disabled":      trade.pyramid_disabled,
        }
        if trade.action == "STRANGLE" and strangle_legs:
            sl = strangle_legs
            state["strangle_legs"] = {
                "ce_strike":        sl.ce_strike,
                "ce_symbol":        sl.ce_symbol,
                "ce_entry_premium": sl.ce_entry_premium,
                "pe_strike":        sl.pe_strike,
                "pe_symbol":        sl.pe_symbol,
                "pe_entry_premium": sl.pe_entry_premium,
                "ce_active":        sl.ce_active,
                "pe_active":        sl.pe_active,
                "hedge_ce_strike":        sl.hedge_ce_strike,
                "hedge_ce_symbol":        sl.hedge_ce_symbol,
                "hedge_ce_entry_premium": sl.hedge_ce_entry_premium,
                "hedge_pe_strike":        sl.hedge_pe_strike,
                "hedge_pe_symbol":        sl.hedge_pe_symbol,
                "hedge_pe_entry_premium": sl.hedge_pe_entry_premium,
            }

    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.error(f"Failed to save trade state: {e}")


def _restore_positions_from_kite():
    """
    On startup, restore the algo's own trade ONLY from what this process
    itself persisted (_STATE_FILE) — not by scanning Kite positions and
    guessing, which can't distinguish the algo's trade from one placed
    manually in the same account. Cross-checks against live positions so a
    stale/closed trade doesn't get resurrected.
    """
    global strangle_legs, _day_realised_pnl, _day_profit_locked, _day_pnl_reset_date

    if not _STATE_FILE.exists():
        logger.info("No persisted trade state — starting flat.")
        return

    try:
        state = json.loads(_STATE_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to read persisted trade state: {e}")
        return

    if state.get("date") == datetime.now().strftime("%Y-%m-%d"):
        _day_realised_pnl   = float(state.get("day_realised_pnl", 0.0))
        _day_profit_locked  = bool(state.get("day_profit_locked", False))
        _day_pnl_reset_date = datetime.now().date()   # so run_scan doesn't zero it as a "new day"
        if _day_realised_pnl or _day_profit_locked:
            logger.info(
                f"Restored day state: realised ₹{_day_realised_pnl:,.0f}  "
                f"profit-lock {'ARMED' if _day_profit_locked else 'not armed'}"
            )

    t = state.get("trade")
    if not t:
        # Nothing was open when this was saved — a stale empty file from a
        # previous day is just clutter, safe to drop.
        if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
            _STATE_FILE.unlink(missing_ok=True)
        return

    # A real trade was open. Strangles (and any position held past EOD) are
    # meant to survive day rollovers, so the calendar date alone must never
    # discard one — only the live-position check below decides that.

    try:
        kite = _get_kite()
        live_symbols = {p['tradingsymbol'] for p in kite.positions()['net'] if p.get('quantity', 0) != 0}
    except Exception as e:
        logger.error(f"Could not verify persisted trade against live positions: {e}")
        return

    symbols_to_check = t["symbol"].split(" + ") if t["action"] == "STRANGLE" else [t["symbol"]]
    if t.get("hedge_symbol"):
        symbols_to_check.append(t["hedge_symbol"])
    sl_check = state.get("strangle_legs")
    if sl_check:
        symbols_to_check += [s for s in (sl_check.get("hedge_ce_symbol"), sl_check.get("hedge_pe_symbol")) if s]

    missing = [s for s in symbols_to_check if s not in live_symbols]
    if missing:
        logger.warning(
            f"Persisted trade {t['action']} {t['strike']} no longer found live "
            f"(missing: {missing}) — discarding persisted state, starting flat."
        )
        _STATE_FILE.unlink(missing_ok=True)
        _post(
            f"⚠️ *Persisted trade not found live on restart*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Was tracking *{t['action'].replace('_',' ')} {t['strike']}* — "
            f"symbols missing from current positions: `{', '.join(missing)}`\n"
            f"_Treating as already closed. Starting flat — please verify manually._"
        )
        return

    monitor.set_trade(TradeState(
        action                 = t["action"],
        strike                 = t["strike"],
        symbol                 = t["symbol"],
        entry_time             = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S"),
        entry_premium          = t["entry_premium"],
        entry_spot             = t["entry_spot"],
        sl_spot_level          = t["sl_spot_level"],
        expiry                 = t["expiry"],
        entry_put_wall         = t.get("entry_put_wall"),
        entry_call_wall        = t.get("entry_call_wall"),
        entry_resistance       = t.get("entry_resistance"),
        entry_support          = t.get("entry_support"),
        lots                   = t.get("lots", 0),
        hedge_symbol           = t.get("hedge_symbol"),
        hedge_strike           = t.get("hedge_strike"),
        hedge_entry_premium    = t.get("hedge_entry_premium"),
        sl_put                 = t.get("sl_put"),
        peak_unrealised        = t.get("peak_unrealised", 0.0),
        entry_lots             = t.get("entry_lots", t.get("lots", 0)),
        profit_booked_step1    = t.get("profit_booked_step1", False),
        profit_booked_step2    = t.get("profit_booked_step2", False),
        pyramid_confirm_count  = t.get("pyramid_confirm_count", 0),
        pyramid_disabled       = t.get("pyramid_disabled", False),
    ))

    sl = state.get("strangle_legs")
    if sl:
        strangle_legs = StrangleLegState(
            ce_strike        = sl["ce_strike"],
            ce_symbol        = sl["ce_symbol"],
            ce_entry_premium = sl["ce_entry_premium"],
            pe_strike        = sl["pe_strike"],
            pe_symbol        = sl["pe_symbol"],
            pe_entry_premium = sl["pe_entry_premium"],
            ce_active        = sl["ce_active"],
            pe_active        = sl["pe_active"],
            hedge_ce_strike        = sl.get("hedge_ce_strike"),
            hedge_ce_symbol        = sl.get("hedge_ce_symbol"),
            hedge_ce_entry_premium = sl.get("hedge_ce_entry_premium"),
            hedge_pe_strike        = sl.get("hedge_pe_strike"),
            hedge_pe_symbol        = sl.get("hedge_pe_symbol"),
            hedge_pe_entry_premium = sl.get("hedge_pe_entry_premium"),
        )

    tr = monitor.trade
    logger.info(f"Restored {tr.action} {tr.strike}  qty={tr.lots}  hedge={tr.hedge_symbol}")
    _post(
        f"♻️ *Position Restored on Restart*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trade: *{tr.action.replace('_',' ')} {tr.strike}*  qty={tr.lots}\n"
        f"Avg entry: ₹{tr.entry_premium:.2f}  |  Expiry: {tr.expiry}\n"
        f"SL spot: {tr.sl_spot_level:.0f}\n"
        f"_Monitoring resumed from persisted state._"
    )


def _unwind_hedge_slice(kite, oc, opt_type: str, hedge_symbol, hedge_strike, hedge_entry, qty: int, what: str):
    """
    Sell back `qty` shares of a far-OTM hedge after the matching quantity of the
    sold option has been bought back (buy-back FIRST, so the position is never
    briefly under-hedged). A partial exit used to buy back the sold lots and leave
    the WHOLE hedge standing: 3 lots of hedge stayed open against 1 lot of short,
    and the final exit only sold what was left of the short's size, so the booked
    slice's hedge was never closed at all.

    Returns (hedge_exit_ltp, hedge_pnl, ok, note). ok=False means the hedge order
    was rejected (or there was nothing to sell with) — the caller keeps going but
    the note goes into the alert so it can be squared off by hand.
    """
    if not hedge_symbol or not hedge_strike or qty <= 0:
        return None, 0.0, True, ""
    hsd = (oc.call_data if opt_type == "CE" else oc.put_data).get(hedge_strike) if oc else None
    ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
    price = _sell_price(oc, opt_type, hedge_strike) if oc else None
    if price is None:
        # The option chain doesn't carry this far-OTM strike (Oct 27: 21450 PE). With no price the
        # order goes out as a MARKET order, which Kite refuses for NFO via the API — the hedge
        # stayed open and nothing said so. Price it from the live quote instead.
        try:
            q = float(kite.ltp([f"NFO:{hedge_symbol}"])[f"NFO:{hedge_symbol}"]["last_price"])
            if q > 0:
                ltp = ltp or q
                price = max(math.floor((q - 1) / 0.05) * 0.05, 0.05)
        except Exception:
            pass
    if price is None:
        send_error_alert(
            f"⚠️ {what}: no price available for hedge {hedge_symbol} x{qty} — it was NOT sold. "
            f"Square it off manually."
        )
        return None, 0.0, False, f"hedge {hedge_symbol} x{qty} NOT sold (no price)"
    try:
        r = place_sell_order(kite, hedge_symbol, qty, price=price)
    except Exception as e:
        send_error_alert(f"{what}: hedge unwind order error ({hedge_symbol} x{qty}): {e}")
        return None, 0.0, False, f"hedge {hedge_symbol} x{qty} NOT sold ({e})"
    if not r.success:
        send_error_alert(
            f"⚠️ {what}: sold leg bought back but the matching HEDGE {hedge_symbol} x{qty} was NOT sold "
            f"({r.error}) — square it off manually."
        )
        return None, 0.0, False, f"hedge {hedge_symbol} x{qty} NOT sold"
    pnl = (ltp - hedge_entry) * qty if ltp is not None and hedge_entry is not None else 0.0
    px = f"₹{ltp:.2f}" if ltp is not None else "~market"
    return ltp, pnl, True, f"hedge {hedge_symbol} x{qty} sold @ {px}"


def _close_directional_slice(trade, kite, spot, oc_exit, exit_qty, tag, time_label):
    """Buy back `exit_qty` shares of the sold leg (and unwind the matching hedge
    quantity), record P&L, and close the trade."""
    opt_type       = "CE" if trade.action == "CALL_SELL" else "PE"
    exit_ltp       = None
    hedge_exit_ltp = None
    if kite and exit_qty > 0:
        try:
            sd = (oc_exit.call_data if opt_type == "CE" else oc_exit.put_data).get(trade.strike) if oc_exit else None
            exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            ep = _buy_price(oc_exit, opt_type, trade.strike) if oc_exit else None
            r  = place_buy_order(kite, trade.symbol, exit_qty, price=ep)
            if not r.success:
                send_error_alert(f"Force exit order failed: {r.error}")
            if trade.hedge_symbol and trade.hedge_strike and oc_exit:
                hedge_exit_ltp, _, _, _ = _unwind_hedge_slice(
                    kite, oc_exit, opt_type, trade.hedge_symbol, trade.hedge_strike, trade.hedge_entry_premium,
                    exit_qty, f"{tag} exit {trade.action} {trade.strike}")
        except Exception as e:
            send_error_alert(f"Force exit order error: {e}")

    main_pnl  = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * exit_qty
    hedge_pnl = (
        (hedge_exit_ltp - trade.hedge_entry_premium) * exit_qty
        if hedge_exit_ltp is not None and trade.hedge_entry_premium is not None else 0.0
    )
    total_pnl    = main_pnl + hedge_pnl
    pnl_emoji    = "✅" if total_pnl >= 0 else "❌"
    exit_s       = f"₹{exit_ltp:.2f}" if exit_ltp else "~market"
    entry_spot_s = f"`{trade.entry_spot:.0f}`" if trade.entry_spot > 0 else "N/A"
    exited_lots  = exit_qty // NIFTY_LOT_SIZE

    lines = [
        f"🔔 *FORCE EXIT — {time_label}*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Trade: *{trade.action.replace('_', ' ')} {trade.strike}*  Expiry {trade.expiry}",
        f"Entry spot: {entry_spot_s}  |  Exit spot: `{spot:.0f}`",
        f"",
        f"*LEGS*  ({exited_lots} lot{'s' if exited_lots != 1 else ''} / {exit_qty} shares)",
        f"   Sold `{trade.strike}{opt_type}`:  ₹{trade.entry_premium:.2f} → *{exit_s}*   {_fmt_pnl(main_pnl)}",
    ]
    if trade.hedge_symbol and trade.hedge_entry_premium is not None:
        hedge_exit_s = f"₹{hedge_exit_ltp:.2f}" if hedge_exit_ltp else "~market"
        lines.append(f"   Hedge `{trade.hedge_strike}{opt_type}`:  ₹{trade.hedge_entry_premium:.2f} → *{hedge_exit_s}*   {_fmt_pnl(hedge_pnl)}")
    lines += [
        f"",
        f"{pnl_emoji} *Net P&L: {_fmt_pnl(total_pnl)}*",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    _post("\n".join(lines))

    journal_trade = replace(trade, lots=exit_qty)
    _record_trade_exit(journal_trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, total_pnl, spot, tag)

    monitor.clear_trade()
    _save_state()


def eod_flatten():
    """
    EOD_FLATTEN time (config.py) — single full flatten of whatever's open, directional or
    strangle, every trading day. Purely intraday: nothing is ever carried
    overnight, regardless of how close the position is to its own expiry.
    Replaces the old 2:55/3:25 split (which put half the exit 5 minutes from
    close, in the manipulation-prone window).
    """
    if monitor.trade is None:
        return

    trade = monitor.trade
    try:
        kite = _get_kite()
        spot = get_current_nifty_price(kite)
    except Exception:
        kite, spot = None, 0.0

    if kite is not None:
        # Closed by hand already? Then there is nothing to flatten — and placing
        # the exit orders anyway would open a position in the other direction.
        _ext_close_strikes[_trade_key(trade)] = 1      # EOD is one-shot: don't wait for a 2nd sighting
        if _reconcile_open_trade(kite) in ("FLAT", "CLOSED"):
            _save_state()
            return
        trade = monitor.trade
        if trade is None:
            return

    if trade.action == "STRANGLE":
        logger.info(f"EOD flatten: STRANGLE {trade.strike}/{trade.expiry}")
        oc_exit = _fetch_oc_for_trade(kite, trade.expiry) if kite else None
        if oc_exit is None:
            # If we can't get the RIGHT chain, don't guess with the wrong one (that's
            # exactly what mispriced the Aug 24 exit order). Alert loudly; the
            # scheduler runs this once more a couple of minutes later.
            send_error_alert(
                f"🚨 EOD flatten could NOT fetch the option chain matching STRANGLE "
                f"{trade.strike}'s expiry {trade.expiry} — position was NOT closed. "
                f"The bot retries at {_eod_flatten_times()[-1]}; if you see this alert again, "
                f"close it manually before the 15:30 close."
            )
            return
        reason = f"EOD flatten — {_EOD_LABEL} (purely intraday, no overnight holds)"
        decision = ManagementDecision(action=EXIT_FULL, reason=reason, reasons=[reason], score=0)
        attempted = _eod_attempt_snapshot(trade)
        _handle_management_decision(decision, trade, spot, None, None, None, oc_exit, None)
        _eod_register_verify(attempted)
        return

    oc_exit = _fetch_oc_for_trade(kite, trade.expiry) if kite else None
    if oc_exit is None:
        send_error_alert(
            f"🚨 EOD flatten could NOT fetch the option chain matching "
            f"{trade.action} {trade.strike}'s expiry {trade.expiry} — position "
            f"was NOT closed. The bot retries at {_eod_flatten_times()[-1]}; if you see "
            f"this alert again, close it manually before the 15:30 close."
        )
        return

    logger.info(f"EOD flatten: {trade.action} {trade.strike}")
    attempted = _eod_attempt_snapshot(trade)
    _close_directional_slice(trade, kite, spot, oc_exit, trade.lots,
                              tag="EOD_FLATTEN", time_label=_EOD_LABEL)
    _eod_register_verify(attempted)


def _eod_attempt_snapshot(trade: TradeState) -> list:
    """The sold legs (and hedges) an EOD flatten is about to try to close."""
    return [dict(label=f"{l['kind']} {l['strike']}", symbol=l["symbol"], hedge=l.get("hedge_symbol"), qty=trade.lots)
            for l in _trade_leg_specs(trade)]


def _eod_register_verify(attempted: list):
    """Remember what the flatten sent orders for — but only if the bot now
    considers the trade closed (otherwise the retry is still handling it)."""
    if monitor.trade is None and attempted:
        _eod_verify.extend(attempted)


def _why_still_short(orders: list, symbol: str) -> str:
    """One line on what happened to the exit order of a leg that is still open."""
    resting = [o for o in orders if o.get("tradingsymbol") == symbol
               and o.get("transaction_type") == "BUY" and o.get("status") in ("OPEN", "TRIGGER PENDING")]
    if resting:
        o = resting[-1]
        return f"exit BUY {o.get('quantity')} still resting at {o.get('price')} (not filled)"
    rej = [o for o in orders if o.get("tradingsymbol") == symbol
           and o.get("transaction_type") == "BUY" and o.get("status") in ("REJECTED", "CANCELLED")]
    if rej:
        return f"exit order {rej[-1].get('status')}: {rej[-1].get('status_message') or 'no reason given'}"
    return "no exit order found"


def _chase_exit(kite, symbol: str, qty_cap: int, attempt: int, orders: list, live_qty: int):
    """
    Re-price an exit that did not fill. Returns (acted: bool, text: str).

    Safety rails — this places REAL orders on its own:
      * only ever buys back what is actually still short, and never more than
        `qty_cap` (the leg's own size), so a manual short in the same symbol
        is never touched beyond the bot's quantity;
      * a resting exit order is MODIFIED in place (atomic) — never a second
        order next to it; a fresh order is placed only when nothing is resting;
      * skipped if the symbol is now part of a live trade (re-entered);
      * limit rises with each attempt (EXIT_CHASE_BUFFER_PCT) and is based on a
        fresh LTP; NFO market orders are not available through the API.
    """
    if not EXIT_CHASE_ENABLED:
        return False, ""
    if attempt >= len(EXIT_CHASE_BUFFER_PCT):
        return False, "chase attempts used up"
    if live_qty >= 0:
        return False, ""
    # never buy back a symbol the bot has since re-entered as a NEW live trade
    live_syms = set()
    t = monitor.trade
    if t is not None:
        live_syms.update(l["symbol"] for l in _trade_leg_specs(t))
    if symbol in live_syms:
        return False, "symbol is part of a new live trade — not chasing"
    try:
        ltp = float(kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"])
    except Exception as e:
        return False, f"no live price ({e}) — could not re-price"
    if ltp <= 0:
        return False, "no live price — could not re-price"

    price = max(ltp * (1 + EXIT_CHASE_BUFFER_PCT[attempt]), ltp + EXIT_CHASE_MIN_BUFFER)
    need  = min(abs(live_qty), qty_cap)
    resting = [o for o in orders if o.get("tradingsymbol") == symbol
               and o.get("transaction_type") == "BUY" and o.get("status") in ("OPEN", "TRIGGER PENDING")]
    pending = sum(int(o.get("pending_quantity", o.get("quantity", 0)) or 0) for o in resting)
    n = f"attempt {attempt + 1}/{len(EXIT_CHASE_BUFFER_PCT)}"

    if resting:
        if pending > need:
            return False, f"resting exit is for {pending} but only {need} is needed — not touching it"
        results = [modify_order_price(kite, o, price) for o in resting]
        failed = [r.error for r in results if not r.success]
        text = f"re-priced the resting exit to ₹{price:.2f} ({n}, market {ltp})"
        if failed:
            return False, f"could not re-price the resting exit: {failed[0]}"
        if pending < need:                                  # part of it already filled / order smaller than needed
            extra = place_buy_limit_exact(kite, symbol, need - pending, price)
            text += f" and bought the remaining {need - pending}" if extra.success else f"; remaining {need - pending} FAILED: {extra.error}"
        return True, text

    r = place_buy_limit_exact(kite, symbol, need, price)
    if r.success:
        return True, f"placed a new exit BUY {need} @ ₹{price:.2f} ({n}, market {ltp})"
    return False, f"new exit order FAILED: {r.error}"


def _register_exit_verify(tag: str, trade: TradeState):
    """Queue a stop-type exit (SL / day floor) for a broker fill check."""
    if not trade.symbol or not trade.lots:
        return
    kind = {"SL_HIT": "SL exit", "DAILY_LOSS_BREAKER": "Daily-loss exit", "DAY_PROFIT_LOCK": "Profit-lock exit"}.get(tag, tag)
    _exit_verify.append(dict(
        kind=kind, symbol=trade.symbol, hedge=trade.hedge_symbol, qty=trade.lots,
        label=trade.action.replace("_", " ") + f" {trade.strike}", t=datetime.now(), alerts=0, last_alert=None,
    ))


def verify_exit_fills(now: Optional[datetime] = None):
    """
    Same "accepted != filled" check as the EOD flatten, for stop-type exits.
    Runs every minute; looks at each queued exit once it is
    EXIT_VERIFY_DELAY_SEC old. Flat → one-line confirmation. Still short →
    alert naming the leg and why, repeated every EXIT_VERIFY_REALERT_MIN until
    it fills or EXIT_VERIFY_GIVE_UP_MIN passes. ALERT ONLY — places no orders.
    Silent when nothing is queued.
    """
    global _exit_verify
    if not _exit_verify:
        return
    now = now or datetime.now()
    if not _is_market_open():
        _exit_verify = []
        return
    due = [e for e in _exit_verify if (now - e["t"]).total_seconds() >= EXIT_VERIFY_DELAY_SEC]
    if not due:
        return
    try:
        kite = _get_kite()
        live = {p["tradingsymbol"]: p.get("quantity", 0) for p in kite.positions()["net"]}
        orders = kite.orders()
    except Exception as e:
        logger.warning(f"Exit fill check: could not reach the broker ({e}) — will retry")
        return

    for e in due:
        age_min = (now - e["t"]).total_seconds() / 60
        q = live.get(e["symbol"], 0)
        if q >= 0:                                   # sold leg is gone
            if e["alerts"]:
                _post(f"✅ *{e['kind']} now filled* — {e['label']} is flat at the broker.")
            else:
                _post(f"✅ *{e['kind']} verified* — {e['label']} is flat at the broker.")
            _exit_verify.remove(e)
            continue

        giving_up = age_min >= EXIT_VERIFY_GIVE_UP_MIN
        acted, chase_text = False, ""
        if not giving_up:
            acted, chase_text = _chase_exit(kite, e["symbol"], e["qty"], e.get("chases", 0), orders, q)
            if acted:
                e["chases"] = e.get("chases", 0) + 1
        due_alert = (acted or e["last_alert"] is None
                     or (now - e["last_alert"]).total_seconds() / 60 >= EXIT_VERIFY_REALERT_MIN)
        if due_alert or giving_up:
            try:
                ltp = kite.ltp([f"NFO:{e['symbol']}"])[f"NFO:{e['symbol']}"]["last_price"]
                mkt = f", market now {ltp}"
            except Exception:
                mkt = ""
            send_error_alert(
                f"🚨 {e['kind']} NOT filled: {e['label']} ({e['symbol']}) still SHORT {abs(q)} at the broker "
                f"(net position — includes any manual trades in this symbol) — {_why_still_short(orders, e['symbol'])}{mkt}. "
                + (f"AUTO: the bot {chase_text}. Checking again shortly." if acted else
                   (f"Auto re-price skipped: {chase_text}. " if chase_text else "")
                   + ("Giving up on checking — close it manually NOW." if giving_up
                      else "Close it manually; the bot checks again shortly."))
            )
            e["alerts"] += 1
            e["last_alert"] = now
        if giving_up:
            _exit_verify.remove(e)


def verify_eod_flatten(final: bool = False):
    """
    "Order accepted" is not "order filled" (Aug 24: an exit the bot believed
    done never filled). After the flatten has sent its orders, ask the broker
    whether the sold legs are really gone. Scheduled twice — shortly after the
    flatten and, with final=True, a minute before the close. Silent when
    nothing was flattened.
    """
    global _eod_verify
    if not _eod_verify:
        return
    last = final
    try:
        kite = _get_kite()
        live = {p["tradingsymbol"]: p.get("quantity", 0) for p in kite.positions()["net"]}
        orders = kite.orders()
    except Exception as e:
        send_error_alert(
            f"🚨 EOD flatten: could not verify the exit with the broker ({e}). "
            f"Check your positions manually before the 15:30 close."
        )
        if last:
            _eod_verify = []
        return

    still = []
    for leg in _eod_verify:
        q = live.get(leg["symbol"], 0)
        if q < 0:
            why = _why_still_short(orders, leg["symbol"])
            acted, chase_text = _chase_exit(kite, leg["symbol"], leg["qty"], leg.get("chases", 0), orders, q)
            if acted:
                leg["chases"] = leg.get("chases", 0) + 1
            still.append(
                f"{leg['label']} ({leg['symbol']}) still SHORT {abs(q)} — {why}"
                + (f" — AUTO: the bot {chase_text}" if acted else (f" — auto re-price skipped: {chase_text}" if chase_text else ""))
            )

    if still:
        send_error_alert(
            "🚨 EOD flatten: position NOT closed at the broker! "
            + " | ".join(still)
            + " — close it manually before the 15:30 close."
            + ("" if last else f" The bot checks again at {_eod_verify_times()[-1]}.")
        )
        if last:
            _eod_verify = []
        return

    stray = [leg for leg in _eod_verify if leg.get("hedge") and live.get(leg["hedge"], 0) > 0]
    _post(
        "✅ *EOD flatten verified* — all sold legs are flat at the broker."
        + (f"\nNote: a long hedge is still open ({', '.join(l['hedge'] for l in stray)}) — harmless, but square it off if you like."
           if stray else "")
    )
    _eod_verify = []


def _register_strangle(signal, spot, oc):
    """Store individual strangle leg details for active management."""
    global strangle_legs
    strangle_legs = StrangleLegState(
        ce_strike=signal.call_strike,
        ce_symbol=signal.call_symbol,
        ce_entry_premium=signal.call_premium or 0,
        pe_strike=signal.put_strike,
        pe_symbol=signal.put_symbol,
        pe_entry_premium=signal.put_premium or 0,
        hedge_ce_strike=signal.hedge_call_strike,
        hedge_ce_symbol=signal.hedge_call_symbol,
        hedge_ce_entry_premium=signal.hedge_call_ltp,
        hedge_pe_strike=signal.hedge_put_strike,
        hedge_pe_symbol=signal.hedge_put_symbol,
        hedge_pe_entry_premium=signal.hedge_put_ltp,
    )


def _enter_strangle(kite, signal, qty: int, spot: float, oc, pyramid_disabled: bool = False) -> bool:
    """
    Enter both strangle legs, buying each side's far-OTM margin hedge first
    when the signal found one (falls back to a naked sell for that side
    otherwise, same as the directional entry path). If one side enters but
    the other fails, unwinds whichever side succeeded rather than leaving a
    naked single-sided position no one asked for.
    Returns True and sets monitor.trade/strangle_legs on success.
    """
    if signal.hedge_call_symbol:
        hedge_ce_res, ce_res = place_spread_entry(
            kite, signal.call_symbol, signal.hedge_call_symbol, qty,
            sell_price=signal.call_premium, hedge_price=signal.hedge_call_ltp,
        )
    else:
        hedge_ce_res, ce_res = None, place_sell_order(kite, signal.call_symbol, qty, price=signal.call_premium)

    if signal.hedge_put_symbol:
        hedge_pe_res, pe_res = place_spread_entry(
            kite, signal.put_symbol, signal.hedge_put_symbol, qty,
            sell_price=signal.put_premium, hedge_price=signal.hedge_put_ltp,
        )
    else:
        hedge_pe_res, pe_res = None, place_sell_order(kite, signal.put_symbol, qty, price=signal.put_premium)

    if ce_res.success and pe_res.success:
        monitor.set_trade(TradeState(
            action="STRANGLE",
            strike=signal.call_strike,
            symbol=f"{signal.call_symbol} + {signal.put_symbol}",
            entry_time=datetime.now(),
            entry_premium=(signal.call_premium or 0) + (signal.put_premium or 0),
            entry_spot=spot,
            sl_spot_level=signal.call_sl or spot,
            sl_put=signal.put_sl,
            expiry=signal.expiry,
            lots=qty,
            pyramid_disabled=pyramid_disabled,
        ))
        _register_strangle(signal, spot, oc)
        _save_state()
        return True

    # One side succeeded, the other failed — unwind the successful side
    # rather than leaving a naked single-sided position.
    if ce_res.success:
        place_buy_order(kite, signal.call_symbol, qty, price=signal.call_premium)
        if hedge_ce_res and hedge_ce_res.success:
            place_sell_order(kite, signal.hedge_call_symbol, qty, price=signal.hedge_call_ltp)
    if pe_res.success:
        place_buy_order(kite, signal.put_symbol, qty, price=signal.put_premium)
        if hedge_pe_res and hedge_pe_res.success:
            place_sell_order(kite, signal.hedge_put_symbol, qty, price=signal.hedge_put_ltp)

    errors = [r.error for r in (ce_res, pe_res) if not r.success]
    send_error_alert(f"Strangle entry failed: {'; '.join(errors)}")
    return False


def _sl_crossed(trade: TradeState, spot: float) -> bool:
    """True if spot is through the trade's stop-loss level."""
    if trade.action == "CALL_SELL":
        return spot >= trade.sl_spot_level
    if trade.action == "PUT_SELL":
        return spot <= trade.sl_spot_level
    if trade.action == "STRANGLE":
        return spot >= trade.sl_spot_level or bool(trade.sl_put and spot <= trade.sl_put)
    return False


def _check_sl_hit(kite, trade: TradeState, spot: float, oc=None) -> bool:
    """Auto-exit if spot crossed the SL level. Returns True if trade was closed."""
    global strangle_legs

    if not _sl_crossed(trade, spot):
        return False

    logger.warning(
        f"SL HIT: {trade.action} {trade.strike}  spot={spot:.1f}  SL={trade.sl_spot_level:.1f}"
    )

    if trade.action == "STRANGLE" and strangle_legs:
        legs = strangle_legs
        leg_lines = []
        total_pnl = 0.0
        if legs.ce_active:
            sd = oc.call_data.get(legs.ce_strike) if oc else None
            ce_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            try:
                ep = _buy_price(oc, "CE", legs.ce_strike) if oc else None
                place_buy_order(kite, legs.ce_symbol, trade.lots, price=ep)
                hedge_ce_exit_ltp = None
                if legs.hedge_ce_symbol:
                    hsd = oc.call_data.get(legs.hedge_ce_strike) if oc else None
                    hedge_ce_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                    hp = _sell_price(oc, "CE", legs.hedge_ce_strike) if oc else None
                    place_sell_order(kite, legs.hedge_ce_symbol, trade.lots, price=hp)
            except Exception as e:
                send_error_alert(f"CE SL exit order error: {e}")
                hedge_ce_exit_ltp = None
            ce_main_pnl  = (legs.ce_entry_premium - (ce_exit_ltp or legs.ce_entry_premium)) * trade.lots
            ce_hedge_pnl = (
                (hedge_ce_exit_ltp - legs.hedge_ce_entry_premium) * trade.lots
                if hedge_ce_exit_ltp is not None and legs.hedge_ce_entry_premium is not None else 0.0
            )
            total_pnl += ce_main_pnl + ce_hedge_pnl
            leg_lines.append(f"   Sold `{legs.ce_strike}CE`:  ₹{legs.ce_entry_premium:.2f} → *{f'₹{ce_exit_ltp:.2f}' if ce_exit_ltp else '~market'}*   {_fmt_pnl(ce_main_pnl)}")
            if legs.hedge_ce_symbol:
                leg_lines.append(f"   Hedge `{legs.hedge_ce_strike}CE`:  {_fmt_pnl(ce_hedge_pnl)}")
            ce_leg_trade = TradeState(
                action="STRANGLE_CE", strike=legs.ce_strike, symbol=legs.ce_symbol,
                entry_time=trade.entry_time, entry_premium=legs.ce_entry_premium,
                entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                lots=trade.lots, hedge_symbol=legs.hedge_ce_symbol, hedge_strike=legs.hedge_ce_strike,
                hedge_entry_premium=legs.hedge_ce_entry_premium,
            )
            _record_trade_exit(ce_leg_trade, ce_exit_ltp, hedge_ce_exit_ltp, ce_main_pnl, ce_hedge_pnl,
                                ce_main_pnl + ce_hedge_pnl, spot, "SL_HIT")

        if legs.pe_active:
            sd = oc.put_data.get(legs.pe_strike) if oc else None
            pe_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            try:
                ep = _buy_price(oc, "PE", legs.pe_strike) if oc else None
                place_buy_order(kite, legs.pe_symbol, trade.lots, price=ep)
                hedge_pe_exit_ltp = None
                if legs.hedge_pe_symbol:
                    hsd = oc.put_data.get(legs.hedge_pe_strike) if oc else None
                    hedge_pe_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                    hp = _sell_price(oc, "PE", legs.hedge_pe_strike) if oc else None
                    place_sell_order(kite, legs.hedge_pe_symbol, trade.lots, price=hp)
            except Exception as e:
                send_error_alert(f"PE SL exit order error: {e}")
                hedge_pe_exit_ltp = None
            pe_main_pnl  = (legs.pe_entry_premium - (pe_exit_ltp or legs.pe_entry_premium)) * trade.lots
            pe_hedge_pnl = (
                (hedge_pe_exit_ltp - legs.hedge_pe_entry_premium) * trade.lots
                if hedge_pe_exit_ltp is not None and legs.hedge_pe_entry_premium is not None else 0.0
            )
            total_pnl += pe_main_pnl + pe_hedge_pnl
            leg_lines.append(f"   Sold `{legs.pe_strike}PE`:  ₹{legs.pe_entry_premium:.2f} → *{f'₹{pe_exit_ltp:.2f}' if pe_exit_ltp else '~market'}*   {_fmt_pnl(pe_main_pnl)}")
            if legs.hedge_pe_symbol:
                leg_lines.append(f"   Hedge `{legs.hedge_pe_strike}PE`:  {_fmt_pnl(pe_hedge_pnl)}")
            pe_leg_trade = TradeState(
                action="STRANGLE_PE", strike=legs.pe_strike, symbol=legs.pe_symbol,
                entry_time=trade.entry_time, entry_premium=legs.pe_entry_premium,
                entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                lots=trade.lots, hedge_symbol=legs.hedge_pe_symbol, hedge_strike=legs.hedge_pe_strike,
                hedge_entry_premium=legs.hedge_pe_entry_premium,
            )
            _record_trade_exit(pe_leg_trade, pe_exit_ltp, hedge_pe_exit_ltp, pe_main_pnl, pe_hedge_pnl,
                                pe_main_pnl + pe_hedge_pnl, spot, "SL_HIT")

        pnl_emoji    = "✅" if total_pnl >= 0 else "❌"
        entry_spot_s = f"`{trade.entry_spot:.0f}`" if trade.entry_spot > 0 else "N/A"
        lines = [
            f"🚨 *SL HIT — STRANGLE CLOSED*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"Trade: *STRANGLE {trade.strike}*  Expiry {trade.expiry}",
            f"Entry spot: {entry_spot_s}  →  now `{spot:.0f}`",
            f"",
            f"*LEGS*",
            *leg_lines,
            f"",
            f"{pnl_emoji} *Net P&L: {_fmt_pnl(total_pnl)}* ({trade.lots} shares each leg)",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
        _post("\n".join(lines))
        monitor.clear_trade()
        strangle_legs = None
        return True

    opt_type       = "CE" if trade.action == "CALL_SELL" else "PE"
    exit_ltp       = None
    hedge_exit_ltp = None
    if trade.lots > 0:
        try:
            if oc:
                sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
                exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            ep = _buy_price(oc, opt_type, trade.strike) if oc else None
            place_buy_order(kite, trade.symbol, trade.lots, price=ep)
            if trade.hedge_symbol and trade.hedge_strike:
                hedge_exit_ltp, _, _, _ = _unwind_hedge_slice(
                    kite, oc, opt_type, trade.hedge_symbol, trade.hedge_strike, trade.hedge_entry_premium,
                    trade.lots, f"SL exit {trade.action} {trade.strike}")
        except Exception as e:
            send_error_alert(f"SL exit order error: {e}")

    main_pnl  = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * trade.lots
    hedge_pnl = (
        (hedge_exit_ltp - trade.hedge_entry_premium) * trade.lots
        if hedge_exit_ltp is not None and trade.hedge_entry_premium is not None else 0.0
    )
    total_pnl    = main_pnl + hedge_pnl
    pnl_emoji    = "✅" if total_pnl >= 0 else "❌"
    exit_s       = f"₹{exit_ltp:.2f}" if exit_ltp else "~market"
    entry_spot_s = f"`{trade.entry_spot:.0f}`" if trade.entry_spot > 0 else "N/A"

    lines = [
        f"🚨 *SL HIT — POSITION CLOSED*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Trade: *{trade.action.replace('_', ' ')} {trade.strike}*  Expiry {trade.expiry}",
        f"Entry spot: {entry_spot_s}  →  now `{spot:.0f}`  SL `{trade.sl_spot_level:.0f}`",
        f"",
        f"*LEGS*",
        f"   Sold `{trade.strike}{opt_type}`:  ₹{trade.entry_premium:.2f} → *{exit_s}*   {_fmt_pnl(main_pnl)}",
    ]
    if trade.hedge_symbol and trade.hedge_entry_premium is not None:
        hedge_exit_s = f"₹{hedge_exit_ltp:.2f}" if hedge_exit_ltp else "~market"
        lines.append(f"   Hedge `{trade.hedge_strike}{opt_type}`:  ₹{trade.hedge_entry_premium:.2f} → *{hedge_exit_s}*   {_fmt_pnl(hedge_pnl)}")
    lines += [
        f"",
        f"{pnl_emoji} *Net P&L: {_fmt_pnl(total_pnl)}* ({trade.lots} shares)",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    _post("\n".join(lines))
    _record_trade_exit(trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, total_pnl, spot, "SL_HIT")
    monitor.clear_trade()
    strangle_legs = None
    return True


# ── Broker reconciliation + intra-scan guard ───────────────────────────────────

def _trade_leg_specs(trade: TradeState):
    """The short leg(s) the bot believes are open: list of dicts with symbol,
    strike, entry premium and hedge info. Strangles list only ACTIVE legs."""
    sl = strangle_legs
    if trade.action == "STRANGLE" and sl:
        legs = []
        if sl.ce_active:
            legs.append(dict(kind="CE", symbol=sl.ce_symbol, strike=sl.ce_strike, entry=sl.ce_entry_premium,
                             hedge_symbol=sl.hedge_ce_symbol, hedge_strike=sl.hedge_ce_strike,
                             hedge_entry=sl.hedge_ce_entry_premium))
        if sl.pe_active:
            legs.append(dict(kind="PE", symbol=sl.pe_symbol, strike=sl.pe_strike, entry=sl.pe_entry_premium,
                             hedge_symbol=sl.hedge_pe_symbol, hedge_strike=sl.hedge_pe_strike,
                             hedge_entry=sl.hedge_pe_entry_premium))
        return legs
    return [dict(kind="CE" if trade.action == "CALL_SELL" else "PE", symbol=trade.symbol, strike=trade.strike,
                 entry=trade.entry_premium, hedge_symbol=trade.hedge_symbol, hedge_strike=trade.hedge_strike,
                 hedge_entry=trade.hedge_entry_premium)]


def _external_close_fills(kite, leg: dict, since: datetime):
    """Average real fill prices of the orders that closed `leg` outside the
    bot: BUY of the sold option and SELL of its hedge, completed after `since`
    (the trade's entry time). Returns (exit_ltp, hedge_exit_ltp, qty) — None /
    0 where nothing was found."""
    try:
        orders = [o for o in kite.orders()
                  if o.get("status") == "COMPLETE" and o.get("order_timestamp")
                  and o["order_timestamp"] >= since]
    except Exception as e:
        logger.warning(f"Could not read orders to price an external close: {e}")
        return None, None, 0

    def _wavg(symbol, side):
        os_ = [o for o in orders if o.get("tradingsymbol") == symbol and o.get("transaction_type") == side]
        q = sum(o.get("filled_quantity", 0) for o in os_)
        if not q:
            return None, 0
        return sum(o["average_price"] * o["filled_quantity"] for o in os_) / q, q

    exit_px, qty = _wavg(leg["symbol"], "BUY")
    hedge_px = None
    if leg.get("hedge_symbol"):
        hedge_px, _ = _wavg(leg["hedge_symbol"], "SELL")
    return exit_px, hedge_px, qty


def _journal_external_close(kite, trade: TradeState, leg: dict):
    """The position was closed outside the bot (manual order). Journal it from
    the real fills — so P&L, the day's realised total and the audit trail are
    true — instead of the bot carrying a phantom trade."""
    exit_px, hedge_px, filled = _external_close_fills(kite, leg, trade.entry_time)
    qty = min(filled, trade.lots) if filled else trade.lots
    main_pnl  = (leg["entry"] - exit_px) * qty if exit_px is not None else 0.0
    hedge_pnl = ((hedge_px - leg["hedge_entry"]) * qty
                 if hedge_px is not None and leg.get("hedge_entry") is not None else 0.0)
    try:
        spot = get_current_nifty_price(kite)
    except Exception:
        spot = 0.0

    if trade.action == "STRANGLE":
        leg_trade = TradeState(
            action=f"STRANGLE_{leg['kind']}", strike=leg["strike"], symbol=leg["symbol"],
            entry_time=trade.entry_time, entry_premium=leg["entry"], entry_spot=trade.entry_spot,
            sl_spot_level=trade.sl_spot_level, expiry=trade.expiry, lots=qty,
            hedge_symbol=leg.get("hedge_symbol"), hedge_strike=leg.get("hedge_strike"),
            hedge_entry_premium=leg.get("hedge_entry"),
        )
    else:
        leg_trade = trade
    detail = (
        f"Closed outside the bot — journaled from Kite fills "
        f"(sold leg bought back @ {exit_px if exit_px is not None else 'n/a'}, "
        f"hedge sold @ {hedge_px if hedge_px is not None else 'n/a'})"
        + ("" if exit_px is not None else " — no closing fills found, P&L recorded as 0")
    )
    _record_trade_exit(leg_trade, exit_px, hedge_px, main_pnl, hedge_pnl, main_pnl + hedge_pnl, spot,
                       "MANUAL_EXIT", exit_detail=detail)


def _reconcile_open_trade(kite) -> str:
    """
    Compare the trade the bot THINKS is open with what the broker actually
    holds — the user trades by hand in the same account and sometimes closes
    the bot's position, which the bot used to discover only by placing exit
    orders against a flat position (Oct 13: that opened a naked long CE and a
    naked short hedge).

    Returns:
      "OK"      — still open at the broker (qty adopted down if it was reduced)
      "FLAT"    — not at the broker on this check; caller must NOT place orders
      "CLOSED"  — flat on 2 consecutive checks: journaled MANUAL_EXIT, state cleared
      "UNKNOWN" — couldn't verify; caller carries on as before
    """
    global strangle_legs
    trade = monitor.trade
    if trade is None:
        return "OK"
    try:
        live = {p["tradingsymbol"]: p.get("quantity", 0) for p in kite.positions()["net"]}
    except Exception as e:
        logger.warning(f"Reconcile: could not read broker positions ({e}) — skipping check")
        return "UNKNOWN"

    legs = _trade_leg_specs(trade)
    if not legs:
        return "OK"

    missing = [l for l in legs if live.get(l["symbol"], 0) >= 0]     # short legs have NEGATIVE net qty
    if not missing:
        _ext_close_strikes.pop(_trade_key(trade), None)
        live_qty = min(abs(live[l["symbol"]]) for l in legs)
        if 0 < live_qty < trade.lots:
            logger.warning(f"Reconcile: broker holds {live_qty} but bot tracked {trade.lots} — adopting {live_qty}")
            _post(
                f"⚠️ *Position size changed outside the bot*\n"
                f"{trade.action.replace('_', ' ')} {trade.strike}: broker holds *{live_qty}*, bot tracked "
                f"*{trade.lots}* — continuing with {live_qty}. Please check the hedge quantity."
            )
            trade.lots = live_qty
            _save_state()
        return "OK"

    # Something the bot thinks is open isn't. A resting order may simply not have filled yet — don't touch it.
    try:
        resting = {o.get("tradingsymbol") for o in kite.orders()
                   if o.get("status") in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")}
    except Exception:
        resting = set()
    if any(l["symbol"] in resting for l in missing):
        return "UNKNOWN"

    key = _trade_key(trade)
    _ext_close_strikes[key] = _ext_close_strikes.get(key, 0) + 1
    names = ", ".join(l["symbol"] for l in missing)
    if _ext_close_strikes[key] < 2:
        logger.warning(f"Reconcile: {names} not found at the broker (1st check) — no orders will be placed; re-checking")
        return "FLAT"

    logger.warning(f"Reconcile: {names} flat at the broker on 2 checks — treating as closed outside the bot")
    for leg in missing:
        _journal_external_close(kite, trade, leg)
        if trade.action == "STRANGLE" and strangle_legs:
            if leg["kind"] == "CE":
                strangle_legs.ce_active = False
            else:
                strangle_legs.pe_active = False
    still_open = trade.action == "STRANGLE" and strangle_legs and (strangle_legs.ce_active or strangle_legs.pe_active)
    if not still_open:
        monitor.clear_trade()
        strangle_legs = None
    _ext_close_strikes.pop(key, None)
    _post(
        f"🖐 *Position closed outside the bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{trade.action.replace('_', ' ')} {trade.strike} was no longer open at the broker, so the bot "
        f"has stopped tracking it and placed *no* exit orders.\n"
        f"Journaled as a manual exit from your actual fills."
    )
    _save_state()
    return "CLOSED"


def _trade_key(trade: TradeState) -> str:
    return f"{trade.symbol}|{trade.entry_time:%Y%m%d%H%M%S}"


def _leg_ltps(kite, trade: TradeState):
    """(current_ltp, strangle_ce_ltp, strangle_pe_ltp) from one cheap kite.ltp
    call — the same shape run_scan derives from the option chain."""
    sl = strangle_legs
    syms = []
    if trade.action == "STRANGLE" and sl:
        if sl.ce_active:
            syms.append(sl.ce_symbol)
        if sl.pe_active:
            syms.append(sl.pe_symbol)
    else:
        syms.append(trade.symbol)
    if not syms:
        return None, None, None
    data = kite.ltp([f"NFO:{s}" for s in syms])
    px = lambda s: (data.get(f"NFO:{s}") or {}).get("last_price") or None
    if trade.action == "STRANGLE" and sl:
        return None, (px(sl.ce_symbol) if sl.ce_active else None), (px(sl.pe_symbol) if sl.pe_active else None)
    return px(trade.symbol), None, None


def guard_check():
    """
    Runs every GUARD_INTERVAL_SECONDS while a trade is open. Between the
    15-minute scans it (1) reconciles with the broker, (2) acts on a spot move
    through the SL, and (3) enforces the day floor (-DAILY_MAX_LOSS, or the
    profit-lock floor once armed). Cheap calls only; the heavy exit path (the
    same code the scans use) runs only when a trigger actually fires.
    """
    trade = monitor.trade
    if trade is None or not _is_market_open():
        return
    try:
        kite = _get_kite()
        if _reconcile_open_trade(kite) in ("FLAT", "CLOSED"):
            return
        trade = monitor.trade
        if trade is None:
            return
        spot = get_current_nifty_price(kite)

        sl_crossed = _sl_crossed(trade, spot)
        decision = None
        if not sl_crossed:
            cur, ce, pe = _leg_ltps(kite, trade)
            unrealised = _current_unrealised(trade, strangle_legs, cur, ce, pe)
            if unrealised is None:
                return
            total_today = _day_realised_pnl + unrealised
            if total_today > _day_floor():
                return
            decision = _floor_breach_decision(total_today, unrealised)

        trade_oc = _fetch_oc_for_trade(kite, trade.expiry)
        if trade_oc is None:
            key = f"oc|{_trade_key(trade)}"
            if key not in _guard_alerted:
                _guard_alerted.add(key)
                send_error_alert(
                    f"🚨 Guard: {trade.action} {trade.strike} needs to exit "
                    f"({'SL crossed' if sl_crossed else 'day floor hit'}) but the matching option chain "
                    f"could not be fetched — retrying every minute."
                )
            return

        if sl_crossed:
            logger.warning(f"GUARD: SL crossed between scans — spot {spot:.1f}, exiting {trade.action} {trade.strike}")
            _check_sl_hit(kite, trade, spot, oc=trade_oc)
        else:
            logger.warning(f"GUARD: {decision.reason}")
            _handle_management_decision(decision, trade, spot, None, None, None, trade_oc, None)
        _save_state()
    except Exception as e:
        logger.exception(f"Guard error: {e}")
        key = f"err|{type(e).__name__}"
        if key not in _guard_alerted:
            _guard_alerted.add(key)
            send_error_alert(f"Guard check error: {e}")


def _handle_management_decision(decision, trade, spot, tl, rsi, opt, oc, df):
    """Act on a position management decision — exit leg(s) and optionally enter new trade."""
    global strangle_legs

    if decision.action == HOLD:
        return

    kite = _get_kite()
    qty  = trade.lots   # shares already computed at entry

    logger.info(f"MANAGEMENT ACTION: {decision.action}  reason={decision.reason}")

    # ── Determine new trade to enter (if any) ─────────────────────
    new_signal = None
    if decision.new_action in ("CALL_SELL", "PUT_SELL", "STRANGLE"):
        new_signal = combine_signals(
            tl=tl, rsi=rsi, opt=opt,
            spot_price=spot,
            expiry=oc.weekly_expiry_date,
        )
        if new_signal.action not in (decision.new_action, "STRANGLE"):
            new_signal = None   # signal changed since decision, don't force entry

    # ── Execute strangle leg exits ────────────────────────────────
    opt_type = "CE" if trade.action == "CALL_SELL" else "PE"

    if decision.action == EXIT_CE_LEG and strangle_legs:
        exit_ltp = hedge_exit_ltp = None
        if strangle_legs.ce_active and qty > 0:
            sd = oc.call_data.get(strangle_legs.ce_strike)
            exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            ep = _buy_price(oc, "CE", strangle_legs.ce_strike)
            r = place_buy_order(kite, strangle_legs.ce_symbol, qty, price=ep)
            if not r.success:
                send_error_alert(f"CE leg exit failed: {r.error}")
            if strangle_legs.hedge_ce_symbol:
                hsd = oc.call_data.get(strangle_legs.hedge_ce_strike)
                hedge_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                hp = _sell_price(oc, "CE", strangle_legs.hedge_ce_strike)
                hr = place_sell_order(kite, strangle_legs.hedge_ce_symbol, qty, price=hp)
                if not hr.success:
                    send_error_alert(f"CE hedge unwind failed: {hr.error}")
            main_pnl  = (strangle_legs.ce_entry_premium - (exit_ltp or strangle_legs.ce_entry_premium)) * qty
            hedge_pnl = (
                (hedge_exit_ltp - strangle_legs.hedge_ce_entry_premium) * qty
                if hedge_exit_ltp is not None and strangle_legs.hedge_ce_entry_premium is not None else 0.0
            )
            ce_leg_trade = TradeState(
                action="STRANGLE_CE", strike=strangle_legs.ce_strike, symbol=strangle_legs.ce_symbol,
                entry_time=trade.entry_time, entry_premium=strangle_legs.ce_entry_premium,
                entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                lots=qty, hedge_symbol=strangle_legs.hedge_ce_symbol, hedge_strike=strangle_legs.hedge_ce_strike,
                hedge_entry_premium=strangle_legs.hedge_ce_entry_premium,
            )
            _record_trade_exit(ce_leg_trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, main_pnl + hedge_pnl,
                                spot, "STRANGLE_LEG_EXIT", exit_detail=decision.reason)
        strangle_legs.ce_active = False
        logger.info(f"CE leg {strangle_legs.ce_strike} closed. PE {strangle_legs.pe_strike} running.")

    elif decision.action == EXIT_PE_LEG and strangle_legs:
        exit_ltp = hedge_exit_ltp = None
        if strangle_legs.pe_active and qty > 0:
            sd = oc.put_data.get(strangle_legs.pe_strike)
            exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            ep = _buy_price(oc, "PE", strangle_legs.pe_strike)
            r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=ep)
            if not r.success:
                send_error_alert(f"PE leg exit failed: {r.error}")
            if strangle_legs.hedge_pe_symbol:
                hsd = oc.put_data.get(strangle_legs.hedge_pe_strike)
                hedge_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                hp = _sell_price(oc, "PE", strangle_legs.hedge_pe_strike)
                hr = place_sell_order(kite, strangle_legs.hedge_pe_symbol, qty, price=hp)
                if not hr.success:
                    send_error_alert(f"PE hedge unwind failed: {hr.error}")
            main_pnl  = (strangle_legs.pe_entry_premium - (exit_ltp or strangle_legs.pe_entry_premium)) * qty
            hedge_pnl = (
                (hedge_exit_ltp - strangle_legs.hedge_pe_entry_premium) * qty
                if hedge_exit_ltp is not None and strangle_legs.hedge_pe_entry_premium is not None else 0.0
            )
            pe_leg_trade = TradeState(
                action="STRANGLE_PE", strike=strangle_legs.pe_strike, symbol=strangle_legs.pe_symbol,
                entry_time=trade.entry_time, entry_premium=strangle_legs.pe_entry_premium,
                entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                lots=qty, hedge_symbol=strangle_legs.hedge_pe_symbol, hedge_strike=strangle_legs.hedge_pe_strike,
                hedge_entry_premium=strangle_legs.hedge_pe_entry_premium,
            )
            _record_trade_exit(pe_leg_trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, main_pnl + hedge_pnl,
                                spot, "STRANGLE_LEG_EXIT", exit_detail=decision.reason)
        strangle_legs.pe_active = False
        logger.info(f"PE leg {strangle_legs.pe_strike} closed. CE {strangle_legs.ce_strike} running.")

    elif decision.action == PROFIT_BOOK:
        book_lots = decision.lots or 0
        book_qty  = book_lots * NIFTY_LOT_SIZE
        if trade.action == "STRANGLE" and strangle_legs:
            legs      = strangle_legs
            ce_exit_ltp = pe_exit_ltp = None
            ce_pnl = pe_pnl = 0.0
            ce_ok = pe_ok = True
            if book_qty > 0 and legs.ce_active:
                sd = oc.call_data.get(legs.ce_strike)
                ce_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                r = place_buy_order(kite, legs.ce_symbol, book_qty, price=_buy_price(oc, "CE", legs.ce_strike))
                if not r.success:
                    send_error_alert(f"Profit-book CE order failed: {r.error}")
                    ce_ok = False
                else:
                    ce_pnl = (legs.ce_entry_premium - (ce_exit_ltp or legs.ce_entry_premium)) * book_qty
            if book_qty > 0 and legs.pe_active:
                sd = oc.put_data.get(legs.pe_strike)
                pe_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                r = place_buy_order(kite, legs.pe_symbol, book_qty, price=_buy_price(oc, "PE", legs.pe_strike))
                if not r.success:
                    send_error_alert(f"Profit-book PE order failed: {r.error}")
                    pe_ok = False
                else:
                    pe_pnl = (legs.pe_entry_premium - (pe_exit_ltp or legs.pe_entry_premium)) * book_qty
            if not (ce_ok and pe_ok):
                # Both legs share a single trade.lots — if only one side's buy-back
                # actually filled, decrementing lots would understate what's still
                # short on the other side and leave a naked residual at exit time.
                send_error_alert(
                    f"⚠️ Profit-book PARTIALLY filled on STRANGLE {legs.ce_strike}/{legs.pe_strike}: "
                    f"CE={'OK' if ce_ok else 'FAILED'}  PE={'OK' if pe_ok else 'FAILED'}"
                    f" — trade.lots NOT updated, please reconcile manually against broker positions."
                )
                return
            locked_pnl = ce_pnl + pe_pnl
            # Both short legs are covered — now take the matching slice off each leg's hedge.
            hedge_pnl, hedge_notes = 0.0, []
            if book_qty > 0 and legs.ce_active:
                _, hp_, _, note_ = _unwind_hedge_slice(kite, oc, "CE", legs.hedge_ce_symbol, legs.hedge_ce_strike,
                                                       legs.hedge_ce_entry_premium, book_qty,
                                                       f"Profit-book STRANGLE CE {legs.ce_strike}")
                hedge_pnl += hp_; hedge_notes.append(note_) if note_ else None
            if book_qty > 0 and legs.pe_active:
                _, hp_, _, note_ = _unwind_hedge_slice(kite, oc, "PE", legs.hedge_pe_symbol, legs.hedge_pe_strike,
                                                       legs.hedge_pe_entry_premium, book_qty,
                                                       f"Profit-book STRANGLE PE {legs.pe_strike}")
                hedge_pnl += hp_; hedge_notes.append(note_) if note_ else None
            step = 1 if not trade.profit_booked_step1 else 2
            monitor.trade.lots -= book_qty
            if step == 1:
                monitor.trade.profit_booked_step1 = True
            else:
                monitor.trade.profit_booked_step2 = True
            _post(
                f"🔒 *PROFIT BOOKED — step {step}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Booked *{book_lots} lot(s)* off *STRANGLE {legs.ce_strike}/{legs.pe_strike}*\n"
                f"💰 Locked: *{_fmt_pnl(locked_pnl + hedge_pnl)}*\n"
                + "".join(f"🛡 {n}\n" for n in hedge_notes)
                + f"\n"
                f"📌 *{monitor.trade.lots // NIFTY_LOT_SIZE} lot(s) still running*"
                f"{' to EOD' if step == 2 else ''}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info(f"Profit-book step {step}: {book_lots} lot(s) each leg, {monitor.trade.lots} shares remaining")
            journal_trade = replace(trade, lots=book_qty)
            _record_trade_exit(journal_trade, ce_exit_ltp or pe_exit_ltp, None, locked_pnl, hedge_pnl,
                               locked_pnl + hedge_pnl, spot, "PROFIT_BOOK")
        elif book_qty > 0:
            sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
            exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            r = place_buy_order(kite, trade.symbol, book_qty, price=_buy_price(oc, opt_type, trade.strike))
            if not r.success:
                send_error_alert(f"Profit-book order failed: {r.error}")
            else:
                locked_pnl = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * book_qty
                # Take the matching slice of the hedge off too (same quantity as the short just covered).
                hedge_exit_ltp, hedge_pnl, hedge_ok, hedge_note = _unwind_hedge_slice(
                    kite, oc, opt_type, trade.hedge_symbol, trade.hedge_strike, trade.hedge_entry_premium,
                    book_qty, f"Profit-book {trade.action} {trade.strike}",
                )
                step = 1 if not trade.profit_booked_step1 else 2
                monitor.trade.lots -= book_qty
                if step == 1:
                    monitor.trade.profit_booked_step1 = True
                else:
                    monitor.trade.profit_booked_step2 = True
                locked_s = f"₹{exit_ltp:.2f}" if exit_ltp else "~market"
                remaining_lots = monitor.trade.lots // NIFTY_LOT_SIZE
                _post(
                    f"🔒 *PROFIT BOOKED — step {step}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Exited *{book_lots} lot(s)* of {trade.action.replace('_',' ')} {trade.strike}\n"
                    f"Entry ₹{trade.entry_premium:.2f} → Exit *{locked_s}*\n"
                    f"💰 Locked: *{_fmt_pnl(locked_pnl + hedge_pnl)}*"
                    + (f"  (short {_fmt_pnl(locked_pnl)}, hedge {_fmt_pnl(hedge_pnl)})" if trade.hedge_symbol and hedge_ok else "")
                    + "\n"
                    + (f"🛡 Hedge: {hedge_note}\n" if hedge_note else "")
                    + f"\n"
                    f"📌 *{remaining_lots} lot(s) still running*{' to EOD' if step == 2 else ''}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                logger.info(f"Profit-book step {step}: {book_lots} lot(s) exited @ {exit_ltp}, {monitor.trade.lots} shares remaining"
                            + (f" — {hedge_note}" if hedge_note else ""))
                journal_trade = replace(trade, lots=book_qty)
                _record_trade_exit(journal_trade, exit_ltp, hedge_exit_ltp, locked_pnl, hedge_pnl,
                                   locked_pnl + hedge_pnl, spot, "PROFIT_BOOK")
        return   # skip send_management_alert at the bottom

    elif decision.action == PYRAMID_ADD:
        if _paused_now():
            logger.info("PAUSED: pyramid add skipped (no size is added while paused).")
            return
        add_lots = decision.lots or 0
        add_qty  = add_lots * NIFTY_LOT_SIZE
        if add_qty <= 0:
            return
        if trade.action == "STRANGLE" and strangle_legs:
            legs = strangle_legs
            ce_add_prem = pe_add_prem = None
            if legs.ce_active:
                sd = oc.call_data.get(legs.ce_strike)
                ce_add_prem = sd.ltp if sd and sd.ltp > 0 else None
                if ce_add_prem:
                    r = place_sell_order(kite, legs.ce_symbol, add_qty, price=ce_add_prem)
                    if not r.success:
                        send_error_alert(f"Pyramid CE add failed: {r.error}")
                        ce_add_prem = None
                    elif legs.hedge_ce_symbol:
                        hp = _buy_price(oc, "CE", legs.hedge_ce_strike)
                        place_buy_order(kite, legs.hedge_ce_symbol, add_qty, price=hp)
            if legs.pe_active:
                sd = oc.put_data.get(legs.pe_strike)
                pe_add_prem = sd.ltp if sd and sd.ltp > 0 else None
                if pe_add_prem:
                    r = place_sell_order(kite, legs.pe_symbol, add_qty, price=pe_add_prem)
                    if not r.success:
                        send_error_alert(f"Pyramid PE add failed: {r.error}")
                        pe_add_prem = None
                    elif legs.hedge_pe_symbol:
                        hp = _buy_price(oc, "PE", legs.hedge_pe_strike)
                        place_buy_order(kite, legs.hedge_pe_symbol, add_qty, price=hp)
            old_qty = trade.lots
            if ce_add_prem is None or pe_add_prem is None:
                # Both legs share a single trade.lots for exit sizing — if only one
                # leg's add actually filled, bumping lots would understate what's
                # short on one side and cause a wrong-sized buy-back later. Leave
                # lots untouched and flag for manual reconciliation instead.
                send_error_alert(
                    f"⚠️ Pyramid add PARTIALLY filled on STRANGLE {legs.ce_strike}/{legs.pe_strike}: "
                    f"CE={'OK' if ce_add_prem is not None else 'FAILED'}  PE={'OK' if pe_add_prem is not None else 'FAILED'}"
                    f" — trade.lots NOT updated, please reconcile manually against broker positions."
                )
                if ce_add_prem is not None:
                    legs.ce_entry_premium = (legs.ce_entry_premium * old_qty + ce_add_prem * add_qty) / (old_qty + add_qty)
                if pe_add_prem is not None:
                    legs.pe_entry_premium = (legs.pe_entry_premium * old_qty + pe_add_prem * add_qty) / (old_qty + add_qty)
                monitor.trade.pyramid_confirm_count = 0
                _save_state()
                return
            legs.ce_entry_premium = (legs.ce_entry_premium * old_qty + ce_add_prem * add_qty) / (old_qty + add_qty)
            legs.pe_entry_premium = (legs.pe_entry_premium * old_qty + pe_add_prem * add_qty) / (old_qty + add_qty)
            monitor.trade.lots = old_qty + add_qty
            monitor.trade.entry_premium = legs.ce_entry_premium + legs.pe_entry_premium
            monitor.trade.pyramid_confirm_count = 0
            _post(
                f"📈 *PYRAMID ADD*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Added *{add_lots} lot(s)* to *STRANGLE {legs.ce_strike}/{legs.pe_strike}*\n"
                f"New size: *{monitor.trade.lots // NIFTY_LOT_SIZE} lot(s)* (cap {MAX_LOTS_STRANGLE_LEG})\n"
                f"_{decision.reason}_\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info(f"Pyramid add: +{add_lots} lot(s) each leg, now {monitor.trade.lots} shares")
        else:
            sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
            add_prem = sd.ltp if sd and sd.ltp > 0 else None
            if not add_prem:
                logger.warning("Pyramid add skipped: no LTP available")
                return
            r = place_sell_order(kite, trade.symbol, add_qty, price=add_prem)
            if not r.success:
                send_error_alert(f"Pyramid add failed: {r.error}")
                return
            if trade.hedge_symbol and trade.hedge_strike:
                hsd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.hedge_strike)
                hedge_price = hsd.ltp if hsd and hsd.ltp > 0 else _buy_price(oc, opt_type, trade.hedge_strike)
                place_buy_order(kite, trade.hedge_symbol, add_qty, price=hedge_price)
            old_qty = trade.lots
            new_qty = old_qty + add_qty
            monitor.trade.entry_premium = (trade.entry_premium * old_qty + add_prem * add_qty) / new_qty
            monitor.trade.lots = new_qty
            monitor.trade.pyramid_confirm_count = 0
            _post(
                f"📈 *PYRAMID ADD*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Added *{add_lots} lot(s)* to {trade.action.replace('_',' ')} {trade.strike} @ ₹{add_prem:.2f}\n"
                f"New size: *{new_qty // NIFTY_LOT_SIZE} lot(s)*  Blended entry: ₹{monitor.trade.entry_premium:.2f} "
                f"(cap {MAX_LOTS_DIRECTIONAL})\n"
                f"_{decision.reason}_\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info(f"Pyramid add: +{add_lots} lot(s) @ {add_prem}, now {new_qty} shares, blended entry {monitor.trade.entry_premium:.2f}")
        _save_state()
        return   # skip send_management_alert at the bottom

    elif decision.action == EXIT_FULL:
        exit_ltp       = None
        hedge_exit_ltp = None
        if qty > 0:
            if trade.action == "STRANGLE" and strangle_legs:
                if decision.reason.startswith("Target hit"):
                    tag = "STRANGLE_TARGET"
                elif decision.reason.startswith("🛑 Daily loss breaker"):
                    tag = "DAILY_LOSS_BREAKER"
                elif decision.reason.startswith("🔒 Day profit-lock"):
                    tag = "DAY_PROFIT_LOCK"
                elif decision.reason.startswith("EOD flatten"):
                    tag = "EOD_FLATTEN"
                else:
                    tag = "STRANGLE_SIGNAL_EXIT"
                if strangle_legs.ce_active:
                    sd = oc.call_data.get(strangle_legs.ce_strike)
                    ce_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                    r = place_buy_order(kite, strangle_legs.ce_symbol, qty, price=_buy_price(oc, "CE", strangle_legs.ce_strike))
                    if not r.success: send_error_alert(f"CE exit failed: {r.error}")
                    hedge_ce_exit_ltp = None
                    if strangle_legs.hedge_ce_symbol:
                        hsd = oc.call_data.get(strangle_legs.hedge_ce_strike)
                        hedge_ce_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                        hr = place_sell_order(kite, strangle_legs.hedge_ce_symbol, qty, price=_sell_price(oc, "CE", strangle_legs.hedge_ce_strike))
                        if not hr.success: send_error_alert(f"CE hedge unwind failed: {hr.error}")
                    ce_main_pnl  = (strangle_legs.ce_entry_premium - (ce_exit_ltp or strangle_legs.ce_entry_premium)) * qty
                    ce_hedge_pnl = (
                        (hedge_ce_exit_ltp - strangle_legs.hedge_ce_entry_premium) * qty
                        if hedge_ce_exit_ltp is not None and strangle_legs.hedge_ce_entry_premium is not None else 0.0
                    )
                    ce_leg_trade = TradeState(
                        action="STRANGLE_CE", strike=strangle_legs.ce_strike, symbol=strangle_legs.ce_symbol,
                        entry_time=trade.entry_time, entry_premium=strangle_legs.ce_entry_premium,
                        entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                        lots=qty, hedge_symbol=strangle_legs.hedge_ce_symbol, hedge_strike=strangle_legs.hedge_ce_strike,
                        hedge_entry_premium=strangle_legs.hedge_ce_entry_premium,
                    )
                    _record_trade_exit(ce_leg_trade, ce_exit_ltp, hedge_ce_exit_ltp, ce_main_pnl, ce_hedge_pnl,
                                        ce_main_pnl + ce_hedge_pnl, spot, tag)
                if strangle_legs.pe_active:
                    sd = oc.put_data.get(strangle_legs.pe_strike)
                    pe_exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                    r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=_buy_price(oc, "PE", strangle_legs.pe_strike))
                    if not r.success: send_error_alert(f"PE exit failed: {r.error}")
                    hedge_pe_exit_ltp = None
                    if strangle_legs.hedge_pe_symbol:
                        hsd = oc.put_data.get(strangle_legs.hedge_pe_strike)
                        hedge_pe_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                        hr = place_sell_order(kite, strangle_legs.hedge_pe_symbol, qty, price=_sell_price(oc, "PE", strangle_legs.hedge_pe_strike))
                        if not hr.success: send_error_alert(f"PE hedge unwind failed: {hr.error}")
                    pe_main_pnl  = (strangle_legs.pe_entry_premium - (pe_exit_ltp or strangle_legs.pe_entry_premium)) * qty
                    pe_hedge_pnl = (
                        (hedge_pe_exit_ltp - strangle_legs.hedge_pe_entry_premium) * qty
                        if hedge_pe_exit_ltp is not None and strangle_legs.hedge_pe_entry_premium is not None else 0.0
                    )
                    pe_leg_trade = TradeState(
                        action="STRANGLE_PE", strike=strangle_legs.pe_strike, symbol=strangle_legs.pe_symbol,
                        entry_time=trade.entry_time, entry_premium=strangle_legs.pe_entry_premium,
                        entry_spot=trade.entry_spot, sl_spot_level=trade.sl_spot_level, expiry=trade.expiry,
                        lots=qty, hedge_symbol=strangle_legs.hedge_pe_symbol, hedge_strike=strangle_legs.hedge_pe_strike,
                        hedge_entry_premium=strangle_legs.hedge_pe_entry_premium,
                    )
                    _record_trade_exit(pe_leg_trade, pe_exit_ltp, hedge_pe_exit_ltp, pe_main_pnl, pe_hedge_pnl,
                                        pe_main_pnl + pe_hedge_pnl, spot, tag)
            else:
                sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
                exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                r = place_buy_order(kite, trade.symbol, qty, price=_buy_price(oc, opt_type, trade.strike))
                if not r.success: send_error_alert(f"Exit order failed: {r.error}")
                if trade.hedge_symbol and trade.hedge_strike:
                    hedge_exit_ltp, _, _, _ = _unwind_hedge_slice(
                        kite, oc, opt_type, trade.hedge_symbol, trade.hedge_strike, trade.hedge_entry_premium,
                        qty, f"Exit {trade.action} {trade.strike}")
        if trade.action != "STRANGLE":
            main_pnl  = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * qty
            hedge_pnl = (
                (hedge_exit_ltp - trade.hedge_entry_premium) * qty
                if hedge_exit_ltp is not None and trade.hedge_entry_premium is not None else 0.0
            )
            if decision.reason.startswith("🛑 Daily loss breaker"):
                directional_tag = "DAILY_LOSS_BREAKER"
            elif decision.reason.startswith("🔒 Day profit-lock"):
                directional_tag = "DAY_PROFIT_LOCK"
            elif decision.reason.startswith("EOD flatten"):
                directional_tag = "EOD_FLATTEN"
            elif decision.reason.startswith("Trailing profit lock"):
                directional_tag = "PROFIT_LOCK"
            else:
                directional_tag = "SIGNAL_EXIT"
            _record_trade_exit(trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, main_pnl + hedge_pnl, spot,
                                directional_tag, exit_detail=decision.reason)
            # Restrict same-direction re-entry: cool down for a few candles, then
            # allow back only on a fresh STRONG signal (see the entry gate). Not
            # applied for the daily-loss breaker or EOD flatten — those already
            # block (or don't need) further entries through other means.
            if directional_tag == "SIGNAL_EXIT":
                global _signal_exit_blocked, _signal_exit_cooldown
                _signal_exit_blocked  = trade.action
                _signal_exit_cooldown = REENTRY_COOLDOWN_CANDLES
                logger.info(
                    f"Re-entry restricted: {trade.action} cooling down {REENTRY_COOLDOWN_CANDLES} "
                    f"candle(s) after SIGNAL_EXIT, then STRONG-only"
                )
        monitor.clear_trade()
        strangle_legs = None

    elif decision.action in (REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE):
        if qty > 0:
            if trade.action == "STRANGLE" and strangle_legs:
                if strangle_legs.ce_active:
                    r = place_buy_order(kite, strangle_legs.ce_symbol, qty, price=_buy_price(oc, "CE", strangle_legs.ce_strike))
                    if not r.success: send_error_alert(f"CE exit failed: {r.error}")
                    if strangle_legs.hedge_ce_symbol:
                        hr = place_sell_order(kite, strangle_legs.hedge_ce_symbol, qty, price=_sell_price(oc, "CE", strangle_legs.hedge_ce_strike))
                        if not hr.success: send_error_alert(f"CE hedge unwind failed: {hr.error}")
                if strangle_legs.pe_active:
                    r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=_buy_price(oc, "PE", strangle_legs.pe_strike))
                    if not r.success: send_error_alert(f"PE exit failed: {r.error}")
                    if strangle_legs.hedge_pe_symbol:
                        hr = place_sell_order(kite, strangle_legs.hedge_pe_symbol, qty, price=_sell_price(oc, "PE", strangle_legs.hedge_pe_strike))
                        if not hr.success: send_error_alert(f"PE hedge unwind failed: {hr.error}")
            else:
                r = place_buy_order(kite, trade.symbol, qty, price=_buy_price(oc, opt_type, trade.strike))
                if not r.success: send_error_alert(f"Exit order failed: {r.error}")
                if trade.hedge_symbol and trade.hedge_strike:
                    _unwind_hedge_slice(kite, oc, opt_type, trade.hedge_symbol, trade.hedge_strike,
                                        trade.hedge_entry_premium, qty, f"Reversal exit {trade.action} {trade.strike}")
        monitor.clear_trade()
        strangle_legs = None

        if new_signal and new_signal.action != "NO_SIGNAL" and _paused_now():
            logger.warning(f"PAUSED: the old position was closed but {new_signal.action} is NOT being entered.")
            new_signal = None

        if new_signal and new_signal.action != "NO_SIGNAL":
            if new_signal.action == "STRANGLE" and _strangle_entry_allowed():
                new_qty = new_signal.strangle_lots * NIFTY_LOT_SIZE
                if not _enter_strangle(kite, new_signal, new_qty, spot, oc):
                    new_signal = None
            elif new_signal.action in ("CALL_SELL", "PUT_SELL") and _entry_allowed():
                new_qty   = new_signal.lots * NIFTY_LOT_SIZE
                is_call   = new_signal.action == "CALL_SELL"
                hedge_sym    = new_signal.hedge_call_symbol if is_call else new_signal.hedge_put_symbol
                hedge_ltp    = new_signal.hedge_call_ltp    if is_call else new_signal.hedge_put_ltp
                hedge_strike = new_signal.hedge_call_strike if is_call else new_signal.hedge_put_strike
                if hedge_sym:
                    _, sell_res = place_spread_entry(kite, new_signal.symbol, hedge_sym, new_qty,
                                                     sell_price=new_signal.premium, hedge_price=hedge_ltp)
                else:
                    sell_res = place_sell_order(kite, new_signal.symbol, new_qty, price=new_signal.premium)
                if sell_res.success:
                    monitor.set_trade(TradeState(
                        action=new_signal.action,
                        strike=new_signal.strike,
                        symbol=new_signal.symbol,
                        entry_time=datetime.now(),
                        entry_premium=new_signal.premium or 0,
                        entry_spot=spot,
                        sl_spot_level=new_signal.sl_spot_level or spot,
                        expiry=new_signal.expiry,
                        lots=new_qty,
                        hedge_symbol=hedge_sym,
                        hedge_strike=hedge_strike,
                        hedge_entry_premium=hedge_ltp,
                    ))
                    _save_state()
                else:
                    send_error_alert(f"Directional re-entry failed: {sell_res.error}")
                    new_signal = None

    # ── Send Telegram alert ───────────────────────────────────────
    send_management_alert(decision, trade, spot, new_signal)


def run_scan():
    global strangle_legs, _signal_exit_blocked, _signal_exit_reset_date
    global _signal_exit_cooldown, _reentry_count
    global _day_realised_pnl, _day_pnl_reset_date, _day_loss_alerted, _day_profit_locked
    if not _is_market_open():
        logger.info("Market closed — skipping.")
        return

    today = datetime.now().date()
    if _signal_exit_reset_date != today:
        _signal_exit_blocked    = None
        _signal_exit_reset_date = today
        _signal_exit_cooldown   = 0
        _reentry_count          = {"CALL_SELL": 0, "PUT_SELL": 0}
    if _day_pnl_reset_date != today:
        _day_realised_pnl   = 0.0
        _day_pnl_reset_date = today
        _day_loss_alerted   = False
        _day_profit_locked  = False

    _pause_reminder()

    logger.info("═" * 55)
    logger.info(f"Scan @ {datetime.now().strftime('%H:%M:%S')}")

    try:
        kite = _get_kite()

        df   = fetch_nifty_candles(kite)
        _announce_broker_ok()
        spot = get_current_nifty_price(kite)
        oc   = fetch_option_chain(kite=kite)

        if oc is None:
            send_error_alert("Option chain fetch failed.")
            return

        tl_result  = analyse_trendlines(df)
        rsi_result = analyse_rsi_divergence(df)
        opt_signal = analyse_option_signal(
            oc=oc,
            trendline_resistance=tl_result.resistance_level,
            trendline_support=tl_result.support_level,
        )

        # ── Active position management ────────────────────────────
        if monitor.trade is not None:
            # First: is the position really still open at the broker? If it was
            # closed by hand, never place exit orders against a flat position.
            if _reconcile_open_trade(kite) in ("FLAT", "CLOSED"):
                _save_state()
                return
            trade = monitor.trade
            if trade is None:
                _save_state()
                return

            # An open trade may be on a DIFFERENT expiry than "current week" if
            # it was entered via the low-premium next-week fallback (see
            # _apply_low_premium_fallback). Every LTP/SL/exit-price lookup below
            # must use the option chain matching the trade's ACTUAL contract —
            # the same strike number on the wrong week is a completely different
            # contract at a completely different price (Aug 24 incident: a
            # "closed" position never actually filled at the broker because the
            # buy-back was priced off the wrong week's much cheaper contract).
            # If we can't get a matching chain, skip management entirely this
            # scan rather than risk pricing off the wrong one — the next scan
            # retries in 15 minutes.
            trade_oc = _fetch_oc_for_trade(kite, trade.expiry, oc_nearest=oc)
            if trade_oc is None:
                send_error_alert(
                    f"⚠️ Could not fetch option chain matching open {trade.action} "
                    f"{trade.strike} (expiry {trade.expiry}) — skipping management "
                    f"this scan rather than risk pricing off the wrong contract."
                )
                return
            trade_opt = opt_signal if trade_oc is oc else analyse_option_signal(
                oc=trade_oc,
                trendline_resistance=tl_result.resistance_level,
                trendline_support=tl_result.support_level,
            )

            # Auto-exit if SL crossed (before position manager to avoid double processing)
            if _check_sl_hit(kite, trade, spot, oc=trade_oc):
                return

            # Current LTP of sold leg (used by the daily-loss breaker, the
            # profit-booking ladder, pyramiding, and position management below)
            opt_type_live = "CE" if trade.action == "CALL_SELL" else "PE"
            live_sd       = (trade_oc.call_data if opt_type_live == "CE" else trade_oc.put_data).get(trade.strike)
            current_ltp   = live_sd.ltp if live_sd and live_sd.ltp > 0 else None

            # Current LTPs of both strangle legs (for the target-decay check)
            strangle_ce_ltp = strangle_pe_ltp = None
            if trade.action == "STRANGLE" and strangle_legs:
                if strangle_legs.ce_active:
                    ce_sd = trade_oc.call_data.get(strangle_legs.ce_strike)
                    strangle_ce_ltp = ce_sd.ltp if ce_sd and ce_sd.ltp > 0 else None
                if strangle_legs.pe_active:
                    pe_sd = trade_oc.put_data.get(strangle_legs.pe_strike)
                    strangle_pe_ltp = pe_sd.ltp if pe_sd and pe_sd.ltp > 0 else None

            # ── Daily-loss hard stop ────────────────────────────────
            # Force-close the algo's own open position the instant today's
            # realised + unrealised P&L breaches -DAILY_MAX_LOSS, rather than
            # letting it ride to its own point-based SL (which can be well
            # past the cap at larger, pyramided lot sizes).
            unrealised_now = _current_unrealised(trade, strangle_legs, current_ltp, strangle_ce_ltp, strangle_pe_ltp)
            if unrealised_now is not None:
                total_today = _day_realised_pnl + unrealised_now
                if total_today <= _day_floor():
                    breach_decision = _floor_breach_decision(total_today, unrealised_now)
                    logger.warning(
                        f"DAY FLOOR HIT: realised ₹{_day_realised_pnl:,.0f} + unrealised ₹{unrealised_now:,.0f} "
                        f"= ₹{total_today:,.0f} <= ₹{_day_floor():,.0f} — force-closing"
                    )
                    _handle_management_decision(breach_decision, trade, spot, tl_result, rsi_result, trade_opt, trade_oc, df)
                    return

            # Run monitor first — updates reversal_candle_count and other counters
            mon_result = monitor.check(oc=trade_oc, rsi=rsi_result, spot=spot)

            # Position manager: should we adjust/exit/reverse?
            pm_decision = evaluate_position(
                trade=trade,
                tl=tl_result,
                rsi=rsi_result,
                opt=trade_opt,
                strangle_legs=strangle_legs,
                reversal_candle_count=monitor.reversal_candle_count,
                clean_after_hedge_count=monitor.clean_after_hedge_count,
                hedge_active=monitor.hedge_active,
                current_ltp=current_ltp,
                strangle_ce_ltp=strangle_ce_ltp,
                strangle_pe_ltp=strangle_pe_ltp,
            )

            if pm_decision.action != HOLD:
                _handle_management_decision(
                    pm_decision, trade, spot,
                    tl_result, rsi_result, trade_opt, trade_oc, df,
                )
            else:
                if mon_result.has_warning:
                    sl_warnings = [w for w in mon_result.warnings if w.category == "SL_PROXIMITY"]
                    if sl_warnings:
                        send_trade_warning(trade, mon_result, spot)
                final_sig = combine_signals(
                    tl=tl_result, rsi=rsi_result, opt=trade_opt,
                    spot_price=spot, expiry=trade_oc.weekly_expiry_date,
                )
                send_live_pnl_update(trade, spot, trade_oc, signal=final_sig, strangle_legs=strangle_legs)

            # If strangle has only one leg remaining, also check for entry of opposite
            if (monitor.trade and monitor.trade.action == "STRANGLE"
                    and strangle_legs and strangle_legs.remaining_leg is not None):
                logger.info(f"Strangle single leg running: {strangle_legs.remaining_leg}")

        # ── Paused: look, report, do not enter ────────────────────
        elif _entry_allowed() and _paused_now():
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            logger.info(f"PAUSED — not entering. Signal was {final.action} {final.score}/3.")
            send_signal(final, observation=True)

        # ── New entry (no active trade) ───────────────────────────
        elif _entry_allowed() and _day_realised_pnl > _day_floor():
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            logger.info(f"Signal: {final.action}  score={final.score}/3  lots={final.lots}")

            final, pyramid_disabled_flag = _apply_low_premium_fallback(
                kite, final, tl_result, rsi_result, spot
            )
            # Late-day entries are sized down; then protected mode (day profit
            # already banked) can shrink further and switches pyramiding off.
            final = _apply_late_entry_sizing(final)
            final, pyramid_disabled_flag = _apply_profit_lock_sizing(final, spot, pyramid_disabled_flag)

            # Same-direction re-entry control after a SIGNAL_EXIT: cool down for a
            # few candles, then allow back only on a fresh STRONG (3/3) signal,
            # capped per direction per day. Everything else enters normally.
            blocked = final.action in ("CALL_SELL", "PUT_SELL") and final.action == _signal_exit_blocked
            if blocked and _signal_exit_cooldown > 0:
                _signal_exit_cooldown -= 1
                logger.info(f"Re-entry cooldown: {final.action} — {_signal_exit_cooldown} candle(s) left, observing")
                send_signal(final, observation=True)
            elif blocked and final.score < 3:
                logger.info(f"Re-entry needs STRONG (3/3): {final.action} is {final.score}/3 — observing")
                send_signal(final, observation=True)
            elif blocked and _reentry_count.get(final.action, 0) >= MAX_SAME_DIR_REENTRIES:
                logger.info(f"Re-entry cap reached ({_reentry_count[final.action]}/{MAX_SAME_DIR_REENTRIES}) "
                            f"for {final.action} — observing")
                send_signal(final, observation=True)
            else:
                is_reentry = blocked   # a qualified STRONG re-entry past cooldown
                send_signal(final)

                if final.action in ("CALL_SELL", "PUT_SELL"):
                    is_call      = final.action == "CALL_SELL"
                    hedge_sym    = final.hedge_call_symbol if is_call else final.hedge_put_symbol
                    hedge_ltp    = final.hedge_call_ltp    if is_call else final.hedge_put_ltp
                    hedge_strike = final.hedge_call_strike if is_call else final.hedge_put_strike
                    qty          = final.lots * NIFTY_LOT_SIZE
                    if hedge_sym:
                        _, sell_res = place_spread_entry(kite, final.symbol, hedge_sym, qty,
                                                         sell_price=final.premium, hedge_price=hedge_ltp)
                    else:
                        sell_res = place_sell_order(kite, final.symbol, qty, price=final.premium)
                    if sell_res.success:
                        monitor.set_trade(TradeState(
                            action=final.action,
                            strike=final.strike,
                            symbol=final.symbol,
                            entry_time=datetime.now(),
                            entry_premium=final.premium or 0,
                            entry_spot=spot,
                            sl_spot_level=final.sl_spot_level or spot,
                            expiry=final.expiry,
                            lots=qty,
                            hedge_symbol=hedge_sym,
                            hedge_strike=hedge_strike,
                            hedge_entry_premium=hedge_ltp,
                            pyramid_disabled=pyramid_disabled_flag,
                        ))
                        if is_reentry:
                            _reentry_count[final.action] = _reentry_count.get(final.action, 0) + 1
                            logger.info(f"STRONG re-entry #{_reentry_count[final.action]} for {final.action}")
                        # New trade is live — clear the restriction so a non-SIGNAL_EXIT
                        # close (target/SL) can't leave a stale same-direction block.
                        _signal_exit_blocked  = None
                        _signal_exit_cooldown = 0
                        _save_state()
                    else:
                        send_error_alert(f"Entry order failed: {sell_res.error}")

                elif final.action == "STRANGLE" and _strangle_entry_allowed():
                    qty = final.strangle_lots * NIFTY_LOT_SIZE
                    _enter_strangle(kite, final, qty, spot, oc, pyramid_disabled=pyramid_disabled_flag)
                    _signal_exit_blocked  = None
                    _signal_exit_cooldown = 0

        elif _entry_allowed():
            # Reachable only when the day floor has been reached (entry window is
            # open but _day_realised_pnl <= _day_floor()): -DAILY_MAX_LOSS normally,
            # DAY_PROFIT_FLOOR once protected mode is armed.
            if not _day_loss_alerted:
                _day_loss_alerted = True
                if _day_profit_locked:
                    _post(
                        f"🔒 *PROFIT LOCK — NO MORE ENTRIES*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Today's P&L is back to *{_fmt_pnl(_day_realised_pnl)}* "
                        f"(floor ₹{DAY_PROFIT_FLOOR:,}).\n"
                        f"No more entries today. Existing position management continues."
                    )
                    logger.info(f"Profit-lock floor reached: realised ₹{_day_realised_pnl:,.0f} — entries halted")
                else:
                    _post(
                        f"🛑 *DAILY LOSS LIMIT HIT*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Realised P&L today: *{_fmt_pnl(_day_realised_pnl)}*  "
                        f"(limit −₹{DAILY_MAX_LOSS:,})\n"
                        f"No more entries today. Existing position management continues."
                    )
                    logger.info(f"Daily loss breaker tripped: realised ₹{_day_realised_pnl:,.0f} — entries halted")
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            send_signal(final, observation=True)
        else:
            # Outside entry window (before 10:00 or after force-exit): observe only
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            send_signal(final, observation=True)

        _save_state()

    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Scan error: {e}")
        send_error_alert(str(e))


def main():
    logger.info("Nifty Option Selling Bot starting...")
    logger.info(f"Entry: {ENTRY_START_HOUR:02d}:{ENTRY_START_MIN:02d} → {FORCE_EXIT_HOUR:02d}:{FORCE_EXIT_MIN:02d}")
    logger.info(
        f"EOD flatten: {' and retry '.join(_eod_flatten_times())} (fill check {', '.join(_eod_verify_times())}) — everything flattens every day, "
        f"purely intraday (directionals and strangles alike, no overnight holds)"
    )
    logger.info(
        f"Guard: SL / day-floor / broker-reconcile check every {GUARD_INTERVAL_SECONDS}s while a trade is open; "
        f"profit lock arms at ₹{DAY_PROFIT_ARM_PNL:,} banked (max {PROTECTED_MAX_LOTS} lots, floor ₹{DAY_PROFIT_FLOOR:,})"
    )
    logger.info(
        f"Late-entry size cut: entries from {LATE_ENTRY_HOUR:02d}:{LATE_ENTRY_MIN:02d} are sized at "
        f"{int(LATE_ENTRY_SIZE_FACTOR * 100)}% (min 1 lot)"
    )

    if SETTINGS is not None:
        import os, userprofile
        _missing = userprofile.missing_secrets(SETTINGS.broker, os.environ)
        if _missing:
            logger.error(f"Profile for {SETTINGS.name} is loaded but these secrets are missing in {_paths.ENV_FILE}: {', '.join(_missing)} — not starting.")
            sys.exit(2)
        import broker as _broker
        _ok, _why = _broker.live_gate(SETTINGS.broker, SETTINGS.paper, os.environ.get("ANGEL_CLIENT_CODE", ""))
        if not _ok:
            logger.error(f"LIVE ANGEL ORDERS REFUSED: {_why}. Run scripts/verify_angel.py in market hours, or set paper = true in the profile.")
            _post(f"🚫 *Live trading is blocked* — {_why}.\nNothing was traded. The owner must run the Angel verification during market hours "
                  f"(or switch this profile back to paper mode).")
            sys.exit(3)
        logger.info(
            f"User profile: {SETTINGS.name} ({SETTINGS.broker})  capital ₹{SETTINGS.sizing.capital:,.0f}  "
            f"daily limit ₹{SETTINGS.sizing.daily_limit:,.0f}  lots {SETTINGS.sizing.lots_strong}/{SETTINGS.sizing.lots_moderate}"
        )
    start_command_listener()
    if SETTINGS is not None:                       # a per-user instance: greet the user on Telegram (once)
        from notifications import onboarding
        onboarding.send_welcome_once(SETTINGS)
    _restore_positions_from_kite()
    run_scan()

    for minute in [":00", ":15", ":30", ":45"]:
        schedule.every().hour.at(minute).do(run_scan)

    # Flatten, plus one retry minutes later: eod_flatten is one-shot and a no-op when
    # nothing is open, so the retry only matters if the first attempt couldn't get the
    # right option chain (it then alerts and leaves the position open).
    for t in _eod_flatten_times():
        schedule.every().day.at(t).do(eod_flatten)
    # Stop-type exits (SL / day floor) get the same "did it really fill?" check, every minute.
    schedule.every(GUARD_INTERVAL_SECONDS).seconds.do(verify_exit_fills)
    # …and ask the broker whether the flatten really filled ("accepted" != "filled").
    for t in _eod_verify_times():
        schedule.every().day.at(t).do(verify_eod_flatten, final=(t == _eod_verify_times()[-1]))
    schedule.every(GUARD_INTERVAL_SECONDS).seconds.do(guard_check)

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
