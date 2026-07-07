"""
Paper Trading Runner
====================
Run:  python run_paper_trade.py

Identical to the live bot in every way EXCEPT:
  - No real orders placed via Zerodha
  - Tracks virtual positions using live option LTPs
  - Reports P&L to Telegram every 15 minutes
  - Saves trade journal to logs/paper_trades_YYYYMMDD.csv

All signal, management, and exit rules are the same as main.py.
Use this for at least 1–2 weeks before going live.
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule

from auth.kite_login import load_access_token
from config import (
    MARKET_OPEN_HOUR, MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN,
    ENTRY_START_HOUR, ENTRY_START_MIN,
    FORCE_EXIT_HOUR, FORCE_EXIT_MIN,
    STRANGLE_CUTOFF_HOUR, STRANGLE_CUTOFF_MIN,
    FRIDAY_STRANGLE_CUTOFF, STRANGLE_SL_BUFFER,
    SL_BUFFER_POINTS,
    ROLL_THRESHOLD_PTS, ROLL_CUTOFF_HOUR, ROLL_CUTOFF_MIN,
    MAX_ROLLS_PER_DAY, REVERSAL_CONFIRM_CANDLES, CLEAN_CONFIRM_CANDLES,
    DAILY_MAX_LOSS,
)
from data.market_data import get_kite_client, fetch_nifty_candles, get_current_nifty_price
from data.option_chain import fetch_option_chain
from notifications.telegram_bot import (
    send_signal,
    send_paper_entry,
    send_paper_pnl_update,
    send_paper_exit,
    send_paper_session_summary,
    send_management_alert,
    send_level_approach_alert,
    send_error_alert,
    send_paper_roll,
    send_paper_hedge_added,
    send_paper_hedge_removed,
    _post,
)
from data.sr_database import get_nearby_levels
from notifications.telegram_commands import start_command_listener
from paper_trade.paper_trader import PaperTrader
from signals.combiner import combine_signals
from signals.position_manager import (
    evaluate_position, StrangleLegState, RollContext,
    EXIT_CE_LEG, EXIT_PE_LEG, EXIT_FULL,
    REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE, HOLD,
    ROLL_UP, ROLL_DOWN, ADD_HEDGE_LEG, REMOVE_HEDGE_LEG,
    bullish_score, bearish_score,
)
from signals.trade_monitor import TradeState, TradeMonitor
from strategy.option_signal import analyse_option_signal
from strategy.rsi_divergence import analyse_rsi_divergence
from strategy.trendline import analyse_trendlines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/paper_trade.log"),
    ],
)
logger = logging.getLogger("paper")

# ── Shared state ──────────────────────────────────────────────────
_kite         = None
_kite_token   = None
monitor       = TradeMonitor()
strangle_legs: StrangleLegState | None = None
paper: PaperTrader | None = None
# Tracks which S/R levels we've already alerted on in this position
# to avoid repeating the same message every 15 min.
# Cleared whenever a position is entered or exited.
_alerted_sr_levels: set[float] = set()
# Daily max loss circuit breaker — reset at end_of_day_summary
_daily_loss_hit: bool = False

STATE_FILE = Path(__file__).parent / "logs" / "paper_state.json"


def _save_state():
    """Persist open positions + monitor to disk so a restart can recover them."""
    if paper is None:
        return

    def _leg_dict(leg):
        return {
            "option_type":   leg.option_type,
            "strike":        leg.strike,
            "symbol":        leg.symbol,
            "lots":          leg.lots,
            "entry_premium": leg.entry_premium,
            "entry_time":    leg.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "active":        leg.active,
            "exit_premium":  leg.exit_premium,
            "exit_time":     leg.exit_time.strftime("%Y-%m-%d %H:%M:%S") if leg.exit_time else None,
            "exit_reason":   leg.exit_reason,
        }

    def _pos_dict(pos):
        return {
            "position_id": pos.position_id,
            "action":      pos.action,
            "entry_time":  pos.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_spot":  pos.entry_spot,
            "expiry":      pos.expiry,
            "sl_call":     pos.sl_call,
            "sl_put":      pos.sl_put,
            "is_closed":   pos.is_closed,
            "legs":        [_leg_dict(l) for l in pos.legs],
        }

    mt = monitor.trade
    sl = strangle_legs
    state = {
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "id_counter":  paper._id_counter,
        "positions":   [_pos_dict(p) for p in paper.positions],
        "monitor_trade": {
            "action":         mt.action,
            "strike":         mt.strike,
            "symbol":         mt.symbol,
            "entry_time":     mt.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_premium":  mt.entry_premium,
            "entry_spot":     mt.entry_spot,
            "sl_spot_level":  mt.sl_spot_level,
            "expiry":         mt.expiry,
            "entry_put_wall":  mt.entry_put_wall,
            "entry_call_wall": mt.entry_call_wall,
            "entry_resistance": mt.entry_resistance,
            "entry_support":    mt.entry_support,
        } if mt else None,
        "monitor_state": {
            "reversal_candle_count":    monitor.reversal_candle_count,
            "clean_after_hedge_count":  monitor.clean_after_hedge_count,
            "above_resistance_count":   monitor.above_resistance_count,
            "below_support_count":      monitor.below_support_count,
            "hedge_active":             monitor.hedge_active,
            "rolls_today":              monitor.rolls_today,
        },
        "strangle_legs": {
            "ce_strike":        sl.ce_strike,
            "ce_symbol":        sl.ce_symbol,
            "ce_entry_premium": sl.ce_entry_premium,
            "pe_strike":        sl.pe_strike,
            "pe_symbol":        sl.pe_symbol,
            "pe_entry_premium": sl.pe_entry_premium,
            "ce_active":        sl.ce_active,
            "pe_active":        sl.pe_active,
        } if sl else None,
    }
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.error(f"[PAPER] Failed to save state: {e}")


def _load_state():
    """Restore positions + monitor from disk after a restart. No-op if file missing or stale."""
    global strangle_legs
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
            STATE_FILE.unlink(missing_ok=True)
            return

        from paper_trade.paper_trader import PaperLeg, PaperPosition

        paper._id_counter = state["id_counter"]
        paper.positions   = []
        for p in state["positions"]:
            legs = []
            for l in p["legs"]:
                legs.append(PaperLeg(
                    option_type   = l["option_type"],
                    strike        = l["strike"],
                    symbol        = l["symbol"],
                    lots          = l["lots"],
                    entry_premium = l["entry_premium"],
                    entry_time    = datetime.strptime(l["entry_time"], "%Y-%m-%d %H:%M:%S"),
                    active        = l["active"],
                    exit_premium  = l["exit_premium"],
                    exit_time     = datetime.strptime(l["exit_time"], "%Y-%m-%d %H:%M:%S") if l["exit_time"] else None,
                    exit_reason   = l["exit_reason"],
                ))
            paper.positions.append(PaperPosition(
                position_id = p["position_id"],
                action      = p["action"],
                entry_time  = datetime.strptime(p["entry_time"], "%Y-%m-%d %H:%M:%S"),
                entry_spot  = p["entry_spot"],
                expiry      = p["expiry"],
                sl_call     = p["sl_call"],
                sl_put      = p["sl_put"],
                is_closed   = p["is_closed"],
                legs        = legs,
            ))

        mt = state.get("monitor_trade")
        if mt:
            monitor.set_trade(TradeState(
                action          = mt["action"],
                strike          = mt["strike"],
                symbol          = mt["symbol"],
                entry_time      = datetime.strptime(mt["entry_time"], "%Y-%m-%d %H:%M:%S"),
                entry_premium   = mt["entry_premium"],
                entry_spot      = mt["entry_spot"],
                sl_spot_level   = mt["sl_spot_level"],
                expiry          = mt["expiry"],
                entry_put_wall  = mt.get("entry_put_wall"),
                entry_call_wall = mt.get("entry_call_wall"),
                entry_resistance = mt.get("entry_resistance"),
                entry_support    = mt.get("entry_support"),
            ))
        ms = state.get("monitor_state", {})
        monitor.reversal_candle_count   = ms.get("reversal_candle_count", 0)
        monitor.clean_after_hedge_count = ms.get("clean_after_hedge_count", 0)
        monitor.above_resistance_count  = ms.get("above_resistance_count", 0)
        monitor.below_support_count     = ms.get("below_support_count", 0)
        monitor.hedge_active            = ms.get("hedge_active", False)
        monitor.rolls_today             = ms.get("rolls_today", 0)

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
            )

        open_pos = paper.open_position
        if open_pos:
            leg0 = open_pos.active_legs[0] if open_pos.active_legs else None
            logger.info(
                f"[PAPER] State restored: {open_pos.action} "
                f"{'strike '+str(leg0.strike) if leg0 else ''} "
                f"entered {open_pos.entry_time.strftime('%H:%M')}"
            )
            _post(
                f"♻️ *[PAPER] RESTARTED — POSITION RESTORED*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Trade: *{open_pos.action}*  |  Expiry {open_pos.expiry}\n"
                f"Entry spot: `{open_pos.entry_spot:.1f}`  entered {open_pos.entry_time.strftime('%H:%M')}\n"
                f"_Monitoring continues — no orders lost._"
            )
        else:
            logger.info("[PAPER] State file found but no open position to restore.")

    except Exception as e:
        logger.error(f"[PAPER] Failed to restore state: {e}")
        STATE_FILE.unlink(missing_ok=True)


def _get_kite():
    global _kite, _kite_token
    token = load_access_token()
    if _kite is None or token != _kite_token:
        _kite = get_kite_client(token)
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
    now_mins = datetime.now().hour * 60 + datetime.now().minute
    return (ENTRY_START_HOUR * 60 + ENTRY_START_MIN) <= now_mins < (FORCE_EXIT_HOUR * 60 + FORCE_EXIT_MIN)


def _strangle_entry_allowed() -> bool:
    now = datetime.now()
    if now.weekday() == 4 and now.hour >= FRIDAY_STRANGLE_CUTOFF:
        return False
    now_mins = now.hour * 60 + now.minute
    return now_mins < STRANGLE_CUTOFF_HOUR * 60 + STRANGLE_CUTOFF_MIN


def _roll_allowed() -> bool:
    now = datetime.now()
    return now.hour * 60 + now.minute < ROLL_CUTOFF_HOUR * 60 + ROLL_CUTOFF_MIN


# ── Entry helpers ─────────────────────────────────────────────────

def _enter_directional(final, spot, oc, tl_result=None):
    global strangle_legs, _alerted_sr_levels
    strangle_legs = None
    _alerted_sr_levels = set()
    pos = paper.enter_directional(
        action=final.action,
        strike=final.strike,
        symbol=final.symbol,
        premium=final.premium or 0,
        lots=final.lots,
        spot=spot,
        sl_spot=final.sl_spot_level or spot,
        expiry=final.expiry,
    )
    monitor.set_trade(TradeState(
        action=final.action, strike=final.strike, symbol=final.symbol,
        entry_time=datetime.now(), entry_premium=final.premium or 0,
        entry_spot=spot, sl_spot_level=final.sl_spot_level or spot,
        expiry=final.expiry,
    ))
    # Capture entry walls for roll tracking
    if final.action == "PUT_SELL":
        monitor.trade.entry_put_wall   = oc.put_wall
    elif final.action == "CALL_SELL":
        monitor.trade.entry_call_wall  = oc.call_wall
    if tl_result:
        monitor.trade.entry_resistance = tl_result.resistance_level
        monitor.trade.entry_support    = tl_result.support_level
    summary = paper.get_position_summary(pos, spot, oc=oc)
    send_paper_entry(summary, final.reasons)
    _save_state()
    return pos


def _enter_strangle(final, spot, oc):
    global strangle_legs, _alerted_sr_levels
    _alerted_sr_levels = set()
    pos = paper.enter_strangle(
        ce_strike=final.call_strike, ce_symbol=final.call_symbol,
        ce_premium=final.call_premium or 0,
        pe_strike=final.put_strike,  pe_symbol=final.put_symbol,
        pe_premium=final.put_premium or 0,
        lots=final.strangle_lots,
        spot=spot,
        sl_call=final.call_sl or spot + STRANGLE_SL_BUFFER,
        sl_put =final.put_sl  or spot - STRANGLE_SL_BUFFER,
        expiry=final.expiry,
    )
    strangle_legs = StrangleLegState(
        ce_strike=final.call_strike, ce_symbol=final.call_symbol,
        ce_entry_premium=final.call_premium or 0,
        pe_strike=final.put_strike,  pe_symbol=final.put_symbol,
        pe_entry_premium=final.put_premium or 0,
    )
    monitor.set_trade(TradeState(
        action="STRANGLE", strike=final.call_strike,
        symbol=f"{final.call_symbol}+{final.put_symbol}",
        entry_time=datetime.now(),
        entry_premium=(final.call_premium or 0) + (final.put_premium or 0),
        entry_spot=spot, sl_spot_level=final.call_sl or spot,
        expiry=final.expiry,
    ))
    summary = paper.get_position_summary(pos, spot, oc=oc)
    send_paper_entry(summary, final.reasons)
    _save_state()
    return pos


# ── SL / Target check ─────────────────────────────────────────────

def _check_sl_target(pos, spot, oc=None):
    """Check SL and target on current paper position. Return exit reason or None."""
    reason = paper.check_sl_and_target(pos, spot, oc=oc)
    if reason:
        ltp_map = {}
        if oc:
            for leg in pos.active_legs:
                chain = oc.call_data if leg.option_type == "CE" else oc.put_data
                sd = chain.get(leg.strike)
                if sd and sd.ltp > 0:
                    ltp_map[leg.symbol] = sd.ltp
        pnl = paper.exit_all_legs(pos, reason, ltp_map=ltp_map or None)
        monitor.clear_trade()
        global strangle_legs, _alerted_sr_levels
        strangle_legs = None
        _alerted_sr_levels = set()
        summary = paper.get_position_summary(pos, spot, oc=oc)
        send_paper_exit(summary, reason)
        logger.info(f"[PAPER] Position closed via {reason}. P&L: ₹{pnl:,.0f}")
        _save_state()
    return reason


# ── Roll / hedge handlers ─────────────────────────────────────────

def _roll_position(pos, trade, spot, oc, tl_result):
    global strangle_legs, _alerted_sr_levels
    from paper_trade.paper_trader import PaperLeg

    # Find the leg to roll
    target_type = "PE" if trade.action == "PUT_SELL" else "CE"
    legs = [l for l in pos.active_legs if l.option_type == target_type]
    if not legs:
        logger.warning("[PAPER] ROLL: no active leg found to roll")
        return
    old_leg = legs[0]
    old_strike = old_leg.strike

    # Fresh option signal for new strike
    new_opt = analyse_option_signal(
        oc=oc,
        trendline_resistance=tl_result.resistance_level if tl_result else None,
        trendline_support=tl_result.support_level if tl_result else None,
    )

    if trade.action == "PUT_SELL":
        new_strike  = new_opt.best_put_strike
        new_symbol  = new_opt.put_symbol
        new_premium = new_opt.put_ltp or 0
    else:
        new_strike  = new_opt.best_call_strike
        new_symbol  = new_opt.call_symbol
        new_premium = new_opt.call_ltp or 0

    if not new_strike or new_strike == old_strike or new_premium < 5.0:
        logger.info(f"[PAPER] ROLL skipped: new_strike={new_strike} old={old_strike} ltp={new_premium}")
        return

    # Exit old leg
    chain  = oc.put_data if old_leg.option_type == "PE" else oc.call_data
    sd     = chain.get(old_leg.strike)
    old_ltp = sd.ltp if sd and sd.ltp > 0 else None
    locked_pnl = paper.exit_leg(pos, old_leg, "ROLL", ltp_override=old_ltp)

    # Add new leg
    new_leg = PaperLeg(
        option_type=target_type,
        strike=new_strike,
        symbol=new_symbol,
        lots=old_leg.lots,
        entry_premium=new_premium,
        entry_time=datetime.now(),
    )
    pos.legs.append(new_leg)
    pos.is_closed = False
    paper._log_leg(pos, new_leg)

    # Update SL
    if trade.action == "PUT_SELL":
        pos.sl_put = spot - SL_BUFFER_POINTS
    else:
        pos.sl_call = spot + SL_BUFFER_POINTS

    # Update monitor
    monitor.trade.strike        = new_strike
    monitor.trade.symbol        = new_symbol
    monitor.trade.entry_premium = new_premium
    monitor.trade.sl_spot_level = pos.sl_put if trade.action == "PUT_SELL" else pos.sl_call
    if trade.action == "PUT_SELL":
        monitor.trade.entry_put_wall = oc.put_wall
    else:
        monitor.trade.entry_call_wall = oc.call_wall
    monitor.rolls_today += 1
    monitor.above_resistance_count = 0
    monitor.below_support_count    = 0
    monitor.reversal_candle_count  = 0
    _alerted_sr_levels = set()

    _save_state()
    send_paper_roll(
        paper.get_position_summary(pos, spot, oc=oc),
        locked_pnl, old_strike, new_strike, new_premium, spot,
    )
    logger.info(f"[PAPER] ROLL {target_type} {old_strike}→{new_strike} @ ₹{new_premium}  locked=₹{locked_pnl:,.0f}")


def _add_hedge_leg(pos, trade, spot, oc):
    global strangle_legs
    from paper_trade.paper_trader import PaperLeg

    # Get hedge strike from fresh option signal
    new_opt = analyse_option_signal(
        oc=oc,
        trendline_resistance=None,
        trendline_support=None,
    )

    if trade.action == "PUT_SELL":
        hedge_type    = "CE"
        hedge_strike  = new_opt.best_call_strike
        hedge_symbol  = new_opt.call_symbol
        hedge_premium = new_opt.call_ltp or 0
    else:
        hedge_type    = "PE"
        hedge_strike  = new_opt.best_put_strike
        hedge_symbol  = new_opt.put_symbol
        hedge_premium = new_opt.put_ltp or 0

    if not hedge_strike or hedge_premium < 5.0:
        logger.warning(f"[PAPER] ADD_HEDGE_LEG skipped: no valid hedge strike (ltp={hedge_premium})")
        return

    # Add hedge leg
    orig_legs = pos.active_legs
    new_leg = PaperLeg(
        option_type=hedge_type,
        strike=hedge_strike,
        symbol=hedge_symbol,
        lots=orig_legs[0].lots if orig_legs else 1,
        entry_premium=hedge_premium,
        entry_time=datetime.now(),
    )
    pos.legs.append(new_leg)
    pos.is_closed = False
    paper._log_leg(pos, new_leg)

    # Update SL for hedge leg
    if trade.action == "PUT_SELL":
        pos.sl_call = spot + SL_BUFFER_POINTS
    else:
        pos.sl_put  = spot - SL_BUFFER_POINTS

    # Build strangle_legs — original leg + hedge leg
    orig_leg = orig_legs[0] if orig_legs else None
    if trade.action == "PUT_SELL" and orig_leg:
        strangle_legs = StrangleLegState(
            ce_strike=hedge_strike, ce_symbol=hedge_symbol, ce_entry_premium=hedge_premium,
            pe_strike=orig_leg.strike, pe_symbol=orig_leg.symbol, pe_entry_premium=orig_leg.entry_premium,
        )
    elif trade.action == "CALL_SELL" and orig_leg:
        strangle_legs = StrangleLegState(
            ce_strike=orig_leg.strike, ce_symbol=orig_leg.symbol, ce_entry_premium=orig_leg.entry_premium,
            pe_strike=hedge_strike, pe_symbol=hedge_symbol, pe_entry_premium=hedge_premium,
        )

    monitor.hedge_active            = True
    monitor.reversal_candle_count   = 0
    monitor.clean_after_hedge_count = 0

    _save_state()
    send_paper_hedge_added(trade.action, hedge_strike, hedge_premium, spot)
    logger.info(f"[PAPER] HEDGE ADDED {hedge_type} {hedge_strike} @ ₹{hedge_premium}")


def _remove_hedge_leg(pos, trade, spot, oc):
    global strangle_legs

    hedge_type = "CE" if trade.action == "PUT_SELL" else "PE"
    hedge_legs = [l for l in pos.active_legs if l.option_type == hedge_type]

    for leg in hedge_legs:
        chain = oc.call_data if leg.option_type == "CE" else oc.put_data
        sd    = chain.get(leg.strike)
        ltp   = sd.ltp if sd and sd.ltp > 0 else None
        paper.exit_leg(pos, leg, "REMOVE_HEDGE", ltp_override=ltp)
        logger.info(f"[PAPER] HEDGE REMOVED {hedge_type} {leg.strike} @ ₹{ltp or leg.entry_premium}")

    if trade.action == "PUT_SELL":
        pos.sl_call = None
    else:
        pos.sl_put  = None

    strangle_legs                   = None
    monitor.hedge_active            = False
    monitor.clean_after_hedge_count = 0

    _save_state()
    send_paper_hedge_removed(trade.action, spot)


# ── Position management action handler ────────────────────────────

def _handle_management(decision, pos, trade, spot, tl, rsi, opt, oc):
    global strangle_legs
    new_signal = None

    if decision.action in (REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE, EXIT_FULL):
        ltp_map = {}
        if oc:
            for leg in pos.active_legs:
                chain = oc.call_data if leg.option_type == "CE" else oc.put_data
                sd = chain.get(leg.strike)
                if sd and sd.ltp > 0:
                    ltp_map[leg.symbol] = sd.ltp
        pnl = paper.exit_all_legs(pos, f"MANAGED:{decision.action}", ltp_map=ltp_map or None)
        monitor.clear_trade()
        strangle_legs = None
        summary = paper.get_position_summary(pos, spot, oc=oc)
        send_paper_exit(summary, decision.action)

        # Attempt new entry
        if decision.new_action:
            new_signal = combine_signals(tl=tl, rsi=rsi, opt=opt,
                                         spot_price=spot, expiry=oc.weekly_expiry_date)
            if new_signal.action == "STRANGLE" and _strangle_entry_allowed():
                _enter_strangle(new_signal, spot, oc)
            elif new_signal.action in ("CALL_SELL","PUT_SELL") and _entry_allowed():
                _enter_directional(new_signal, spot, oc)
            else:
                new_signal = None

    elif decision.action == EXIT_CE_LEG:
        ce_legs = [l for l in pos.active_legs if l.option_type == "CE"]
        for leg in ce_legs:
            ltp = None
            if oc:
                sd = oc.call_data.get(leg.strike)
                if sd and sd.ltp > 0:
                    ltp = sd.ltp
            paper.exit_leg(pos, leg, "MANAGED:EXIT_CE", ltp_override=ltp)
        if strangle_legs:
            strangle_legs.ce_active = False

    elif decision.action == EXIT_PE_LEG:
        pe_legs = [l for l in pos.active_legs if l.option_type == "PE"]
        for leg in pe_legs:
            ltp = None
            if oc:
                sd = oc.put_data.get(leg.strike)
                if sd and sd.ltp > 0:
                    ltp = sd.ltp
            paper.exit_leg(pos, leg, "MANAGED:EXIT_PE", ltp_override=ltp)
        if strangle_legs:
            strangle_legs.pe_active = False

    elif decision.action == ROLL_UP:
        _roll_position(pos, trade, spot, oc, tl)
        return  # roll handler sends its own Telegram message

    elif decision.action == ROLL_DOWN:
        _roll_position(pos, trade, spot, oc, tl)
        return

    elif decision.action == ADD_HEDGE_LEG:
        _add_hedge_leg(pos, trade, spot, oc)
        return

    elif decision.action == REMOVE_HEDGE_LEG:
        _remove_hedge_leg(pos, trade, spot, oc)
        return

    _save_state()
    send_management_alert(decision, trade, spot, new_signal)


# ── Force exit at 2:55 PM ─────────────────────────────────────────

def force_exit_all():
    global strangle_legs
    pos = paper.open_position if paper else None
    if pos is None:
        return
    try:
        try:
            kite = _get_kite()
            spot = get_current_nifty_price(kite)
            oc   = fetch_option_chain(kite=kite)
        except Exception:
            spot = 0.0
            oc   = None

        if monitor.trade and monitor.trade.action == "STRANGLE":
            active = []
            if strangle_legs:
                if strangle_legs.ce_active: active.append(f"CE {strangle_legs.ce_strike}")
                if strangle_legs.pe_active: active.append(f"PE {strangle_legs.pe_strike}")
            summary = paper.get_position_summary(pos, spot, oc=oc)
            _post(
                f"🌙 *[PAPER] STRANGLE — HOLDING OVERNIGHT*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Active legs: *{' + '.join(active)}*\n"
                f"Entry spot: `{pos.entry_spot:.1f}`  |  Now: `{spot:.1f}`\n"
                f"Unrealised P&L: ₹{summary['unrealised_pnl']:,.0f}\n"
                f"_Monitoring continues tomorrow._"
            )
            return

        # Build LTP map from the option chain so exit pricing doesn't depend
        # on a separate kite.ltp() call that can fail at market close.
        ltp_map = {}
        if oc:
            for leg in pos.active_legs:
                chain = oc.call_data if leg.option_type == "CE" else oc.put_data
                sd = chain.get(leg.strike)
                if sd and sd.ltp > 0:
                    ltp_map[leg.symbol] = sd.ltp

        pnl = paper.exit_all_legs(pos, "FORCE_EXIT_255PM", ltp_map=ltp_map or None)
        monitor.clear_trade()
        strangle_legs = None
        summary = paper.get_position_summary(pos, spot, oc=oc)
        send_paper_exit(summary, "FORCE_EXIT_255PM")
        _save_state()
    except Exception as e:
        logger.exception(f"[PAPER] force_exit_all failed: {e}")
        send_error_alert(f"[PAPER] Force exit error — position may still be open: {e}")


def end_of_day_summary():
    global _daily_loss_hit
    if not paper:
        return
    # Force-exit any position that survived a crash+restart past 14:55
    if paper.open_position:
        logger.warning("[PAPER] Open position found at EOD — running late force exit.")
        force_exit_all()
    summary = paper.session_summary()
    send_paper_session_summary(summary)
    STATE_FILE.unlink(missing_ok=True)
    _daily_loss_hit = False  # reset for next trading day (service runs continuously)


# ── Main scan ─────────────────────────────────────────────────────

def run_scan():
    global strangle_legs, _daily_loss_hit
    if not _is_market_open():
        return

    logger.info("═" * 55)
    logger.info(f"[PAPER] Scan @ {datetime.now().strftime('%H:%M:%S')}")

    try:
        kite = _get_kite()
        df   = fetch_nifty_candles(kite)
        spot = get_current_nifty_price(kite)
        oc   = fetch_option_chain(kite=kite)

        if oc is None:
            send_error_alert("[PAPER] Option chain fetch failed.")
            return

        tl_result  = analyse_trendlines(df)
        rsi_result = analyse_rsi_divergence(df)
        opt_signal = analyse_option_signal(
            oc=oc,
            trendline_resistance=tl_result.resistance_level,
            trendline_support=tl_result.support_level,
        )

        # ── Daily loss circuit breaker ────────────────────────────
        realised_today = sum(sum(l.realised_pnl or 0 for l in p.legs) for p in paper.positions)
        _pos_tmp = paper.open_position
        _unrealised = 0
        if _pos_tmp:
            _tmp_summary = paper.get_position_summary(_pos_tmp, spot, oc=oc)
            _unrealised = _tmp_summary["unrealised_pnl"]
        if realised_today + _unrealised <= -DAILY_MAX_LOSS:
            if not _daily_loss_hit:
                if _pos_tmp:
                    _ltp_map = {}
                    for _leg in _pos_tmp.active_legs:
                        _chain = oc.call_data if _leg.option_type == "CE" else oc.put_data
                        _sd = _chain.get(_leg.strike)
                        if _sd and _sd.ltp > 0:
                            _ltp_map[_leg.symbol] = _sd.ltp
                    paper.exit_all_legs(_pos_tmp, "DAILY_LOSS_LIMIT", ltp_map=_ltp_map or None)
                    monitor.clear_trade()
                    strangle_legs = None
                    _save_state()
                _daily_loss_hit = True
                _post(
                    f"🛑 *[PAPER] DAILY LOSS LIMIT HIT*\n"
                    f"₹{DAILY_MAX_LOSS:,.0f} breached. All positions closed. No more entries today."
                )
                logger.warning(f"[PAPER] Daily loss limit hit: ₹{realised_today+_unrealised:,.0f}")
            return

        pos = paper.open_position

        # ── Active position ───────────────────────────────────────
        if pos and monitor.trade:
            trade = monitor.trade

            # 1. SL / target check first
            if _check_sl_target(pos, spot, oc=oc):
                return   # position closed, nothing more to do

            # 2. Update reversal/clean counters (before evaluate_position)
            _bull, _ = bullish_score(tl_result, rsi_result, opt_signal)
            _bear, _ = bearish_score(tl_result, rsi_result, opt_signal)

            if not monitor.hedge_active:
                reversal = (_bull >= 2 if trade.action == "CALL_SELL" else _bear >= 2)
                if reversal:
                    monitor.reversal_candle_count   += 1
                    monitor.clean_after_hedge_count  = 0
                else:
                    monitor.reversal_candle_count    = 0
                    monitor.clean_after_hedge_count  = 0
            else:  # hedge is active
                clean = (_bear >= 2 if trade.action == "CALL_SELL" else _bull >= 2)
                if clean:
                    monitor.clean_after_hedge_count += 1
                    monitor.reversal_candle_count    = 0
                else:
                    monitor.clean_after_hedge_count  = 0

            # Update breakout counter
            monitor.update_breakout_count(spot, tl_result.resistance_level, tl_result.support_level)

            # Build roll context
            roll_ctx = RollContext(
                put_wall               = oc.put_wall,
                call_wall              = oc.call_wall,
                entry_put_wall         = trade.entry_put_wall,
                entry_call_wall        = trade.entry_call_wall,
                above_resistance_count = monitor.above_resistance_count,
                below_support_count    = monitor.below_support_count,
                rolls_today            = monitor.rolls_today,
                roll_allowed           = _roll_allowed(),
            )

            # 3. Position management
            pm_decision = evaluate_position(
                trade=trade, tl=tl_result, rsi=rsi_result,
                opt=opt_signal, strangle_legs=strangle_legs,
                reversal_candle_count=monitor.reversal_candle_count,
                clean_after_hedge_count=monitor.clean_after_hedge_count,
                hedge_active=monitor.hedge_active,
                roll_ctx=roll_ctx,
            )
            if pm_decision.action != HOLD:
                _handle_management(pm_decision, pos, trade, spot,
                                   tl_result, rsi_result, opt_signal, oc)
            else:
                # Skip P&L update at force exit time — force_exit_all sends the closing message
                # and LTPs are unavailable once the market closes at 15:30
                now = datetime.now()
                if now.hour == FORCE_EXIT_HOUR and now.minute == FORCE_EXIT_MIN:
                    return

                # 4. Check if price is approaching an adverse S/R level
                nearby = get_nearby_levels(spot, trade.action, warning_pct=0.003)
                for lvl in nearby:
                    if lvl.level not in _alerted_sr_levels:
                        send_level_approach_alert(trade.action, lvl, spot)
                        _alerted_sr_levels.add(lvl.level)
                        logger.info(
                            f"[PAPER] Level approach alert sent: "
                            f"{lvl.sr_type} {lvl.level:.0f}  spot={spot:.1f}"
                        )

                # 5. P&L update with live signal dashboard and verdict
                final = combine_signals(
                    tl=tl_result, rsi=rsi_result, opt=opt_signal,
                    spot_price=spot, expiry=oc.weekly_expiry_date,
                )
                summary = paper.get_position_summary(pos, spot, oc=oc)
                send_paper_pnl_update(summary, final)
                logger.info(
                    f"[PAPER] Unrealised P&L: ₹{summary['unrealised_pnl']:,.0f}  "
                    f"spot={spot:.1f}"
                )

        # ── No position — look for entry ──────────────────────────
        elif not _daily_loss_hit and _entry_allowed():
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            # Always send the market scan to Telegram
            send_signal(final)

            if final.action in ("CALL_SELL", "PUT_SELL"):
                _enter_directional(final, spot, oc, tl_result)
            elif final.action == "STRANGLE" and _strangle_entry_allowed():
                _enter_strangle(final, spot, oc)
        else:
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            if final.action == "NO_SIGNAL":
                send_signal(final)

    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception(f"[PAPER] Scan error: {e}")
        send_error_alert(f"[PAPER] {e}")


def main():
    global paper
    kite  = _get_kite()
    paper = PaperTrader(kite)
    _load_state()

    logger.info("=" * 55)
    logger.info("  PAPER TRADING MODE — No real orders will be placed")
    logger.info("=" * 55)

    start_command_listener()

    if not paper.open_position:
        _post(
            "📝 *PAPER TRADING STARTED*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "All signals, position management and exit rules are live.\n"
            "No real orders placed — P&L tracked virtually.\n"
            f"Journal: `logs/paper_trades_{datetime.now().strftime('%Y%m%d')}.csv`\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Run for 1–2 weeks, then switch to `python main.py` for live trading._"
        )

    run_scan()

    for minute in [":00", ":15", ":30", ":45"]:
        schedule.every().hour.at(minute).do(run_scan)

    schedule.every().day.at("14:55").do(force_exit_all)
    schedule.every().day.at("15:35").do(end_of_day_summary)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
