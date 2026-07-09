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

Strangles held overnight. Directionals force-exited at 2:55 PM — split
across 2:55/3:25 PM (ceil half now, floor half at 3:25) when 2+ lots are open.
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

from auth.kite_login import load_access_token
from execution.order_manager import place_sell_order, place_buy_order, place_spread_entry, square_off_position
from config import (
    MARKET_OPEN_HOUR, MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN,
    ENTRY_START_HOUR, ENTRY_START_MIN,
    FORCE_EXIT_HOUR, FORCE_EXIT_MIN,
    STRANGLE_CUTOFF_HOUR, STRANGLE_CUTOFF_MIN,
    FRIDAY_STRANGLE_CUTOFF, STRANGLE_SL_BUFFER,
    NIFTY_LOT_SIZE, TOTAL_MTM_MAX_LOSS,
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
    evaluate_position, StrangleLegState,
    EXIT_CE_LEG, EXIT_PE_LEG, EXIT_FULL,
    REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE, HOLD,
    PARTIAL_PROFIT_LOCK,
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
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger("main")

JOURNAL_PATH = Path("logs/trade_journal.csv")
JOURNAL_FIELDS = [
    "date", "exit_time", "action", "strike", "expiry",
    "entry_time", "entry_spot", "exit_spot",
    "entry_premium", "exit_premium",
    "hedge_strike", "hedge_entry_premium", "hedge_exit_premium",
    "lots", "main_pnl", "hedge_pnl", "net_pnl", "exit_reason",
]


def _fmt_pnl(p: float) -> str:
    return f"+₹{p:,.0f}" if p >= 0 else f"−₹{abs(p):,.0f}"


def _record_trade_exit(
    trade: "TradeState",
    exit_ltp: Optional[float],
    hedge_exit_ltp: Optional[float],
    main_pnl: float,
    hedge_pnl: float,
    total_pnl: float,
    spot: float,
    exit_reason: str,
):
    """Append trade to CSV journal and send Telegram record."""
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

    send_trade_journal_entry(trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, total_pnl, spot, exit_reason)


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
_signal_exit_blocked: Optional[str]   = None    # direction blocked for rest of session after SIGNAL_EXIT
_total_loss_hit: bool = False    # True once the whole-account MTM guard has squared off for the day
_total_loss_reset_date = None   # last date _total_loss_hit was reset — detects day rollover


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


_STATE_FILE = Path(__file__).parent / "logs" / "algo_trade_state.json"


def _save_state():
    """
    Persist exactly what THIS bot placed to disk. This is the only source of
    truth used to restore state on restart — never re-derived by guessing
    from whatever happens to be open in the Kite account, since that can't
    be told apart from a position placed manually in the same account.
    """
    trade = monitor.trade
    state = {"date": datetime.now().strftime("%Y-%m-%d"), "trade": None, "strangle_legs": None}

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
            "partial_profit_locked": trade.partial_profit_locked,
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
    global strangle_legs

    if not _STATE_FILE.exists():
        logger.info("No persisted trade state — starting flat.")
        return

    try:
        state = json.loads(_STATE_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to read persisted trade state: {e}")
        return

    if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
        logger.info("Persisted trade state is from a previous day — discarding.")
        _STATE_FILE.unlink(missing_ok=True)
        return

    t = state.get("trade")
    if not t:
        return

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
        partial_profit_locked  = t.get("partial_profit_locked", False),
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


def _check_total_mtm():
    """
    Every scan: sum live P&L across every open position in the account —
    the algo's own trade included, no distinction made — and square off
    everything the moment the combined loss exceeds -TOTAL_MTM_MAX_LOSS.
    Fires once per day.
    """
    global _total_loss_hit, _total_loss_reset_date, strangle_legs

    today = datetime.now().date()
    if _total_loss_reset_date != today:
        _total_loss_hit        = False
        _total_loss_reset_date = today

    if _total_loss_hit:
        return

    try:
        kite    = _get_kite()
        all_pos = kite.positions()['net']
    except Exception as e:
        logger.warning(f"Total MTM check: positions() failed: {e}")
        return

    open_positions = [p for p in all_pos if p.get('quantity', 0) != 0]
    if not open_positions:
        return

    total_pnl = sum(p.get('pnl', 0) for p in open_positions)
    logger.info(f"Total account MTM: {_fmt_pnl(total_pnl)}  ({len(open_positions)} position(s))")

    if total_pnl > -TOTAL_MTM_MAX_LOSS:
        return

    logger.warning(
        f"Total MTM loss breach: {_fmt_pnl(total_pnl)}  "
        f"≤  -₹{TOTAL_MTM_MAX_LOSS:,} — squaring off everything"
    )
    closed = []
    for p in open_positions:
        symbol   = p['tradingsymbol']
        qty      = p['quantity']
        exchange = p.get('exchange') or 'NFO'
        product  = p.get('product') or 'NRML'
        ltp      = p.get('last_price') or None
        result = square_off_position(kite, symbol, qty, exchange=exchange, product=product, ltp=ltp)
        if result.success:
            closed.append((symbol, abs(qty), p.get('pnl', 0)))
        else:
            send_error_alert(f"Square-off failed for {symbol}: {result.error}")

    _total_loss_hit = True
    monitor.clear_trade()
    strangle_legs = None
    _save_state()

    lines = [
        f"🚨 *ALL POSITIONS SQUARED OFF — DAILY LOSS LIMIT*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Combined MTM {_fmt_pnl(total_pnl)} breached -₹{TOTAL_MTM_MAX_LOSS:,} across all open positions.",
        f"",
    ]
    for sym, qty, pnl in closed:
        lines.append(f"   `{sym}`  qty={qty}   {_fmt_pnl(pnl)}")
    lines += [
        f"",
        f"_No more entries today._",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    _post("\n".join(lines))


def _close_directional_slice(trade, kite, spot, oc_exit, exit_qty, tag, time_label, remaining_qty=0):
    """
    Buy back `exit_qty` shares of the sold leg (and unwind the matching hedge
    quantity), record P&L for that slice, and either close the trade fully
    (remaining_qty == 0) or leave it open with `remaining_qty` shares running.
    """
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
                hsd = (oc_exit.call_data if opt_type == "CE" else oc_exit.put_data).get(trade.hedge_strike)
                hedge_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                hp = _sell_price(oc_exit, opt_type, trade.hedge_strike)
                place_sell_order(kite, trade.hedge_symbol, exit_qty, price=hp)
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

    header = f"🔔 *FORCE EXIT — {time_label}*" if remaining_qty == 0 else f"🔔 *PARTIAL FORCE EXIT — {time_label}*"
    lines = [
        header,
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
    ]
    if remaining_qty > 0:
        remaining_lots = remaining_qty // NIFTY_LOT_SIZE
        lines.append(f"📌 *{remaining_lots} lot{'s' if remaining_lots != 1 else ''} still running — exits at 3:25 PM*")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    _post("\n".join(lines))

    journal_trade = replace(trade, lots=exit_qty)
    _record_trade_exit(journal_trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, total_pnl, spot, tag)

    if remaining_qty > 0:
        monitor.trade.lots = remaining_qty
    else:
        monitor.clear_trade()
    _save_state()


def force_exit_all():
    """2:55 PM — close directional trades. Splits the exit across 2:55/3:25 PM
    when 2+ lots are open (ceil half now, floor half at 3:25). Strangles held overnight."""
    if monitor.trade is None:
        return

    trade = monitor.trade
    try:
        kite = _get_kite()
        spot = get_current_nifty_price(kite)
    except Exception:
        kite = None
        spot = 0.0

    if trade.action == "STRANGLE":
        active_legs = []
        if strangle_legs:
            if strangle_legs.ce_active:
                active_legs.append(f"CE {strangle_legs.ce_strike}")
            if strangle_legs.pe_active:
                active_legs.append(f"PE {strangle_legs.pe_strike}")
        _post(
            f"🌙 *STRANGLE — HOLDING OVERNIGHT*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Active legs: *{' + '.join(active_legs)}*\n"
            f"Entry spot: `{trade.entry_spot:.1f}`  |  Now: `{spot:.1f}`\n"
            f"Expiry: {trade.expiry}\n"
            f"_SL and target monitoring continues tomorrow._"
        )
        return

    oc_exit = None
    if kite:
        try:
            oc_exit = fetch_option_chain(kite=kite)
        except Exception:
            oc_exit = None

    num_lots = trade.lots // NIFTY_LOT_SIZE
    if num_lots <= 1:
        logger.info(f"2:55 PM force exit: {trade.action} {trade.strike}")
        _close_directional_slice(trade, kite, spot, oc_exit, trade.lots,
                                  tag="FORCE_EXIT_255PM", time_label="2:55 PM", remaining_qty=0)
        return

    first_lots  = -(-num_lots // 2)          # ceil(num_lots / 2)
    second_lots = num_lots - first_lots
    exit_qty    = first_lots  * NIFTY_LOT_SIZE
    remain_qty  = second_lots * NIFTY_LOT_SIZE
    logger.info(
        f"2:55 PM partial force exit: {trade.action} {trade.strike}  "
        f"{first_lots} lot(s) now, {second_lots} lot(s) held to 3:25 PM"
    )
    _close_directional_slice(trade, kite, spot, oc_exit, exit_qty,
                              tag="FORCE_EXIT_255PM", time_label="2:55 PM", remaining_qty=remain_qty)


def force_exit_remaining():
    """3:25 PM — close whatever's left of a directional trade after the 2:55 PM partial exit."""
    if monitor.trade is None or monitor.trade.action == "STRANGLE":
        return

    trade = monitor.trade
    try:
        kite    = _get_kite()
        spot    = get_current_nifty_price(kite)
        oc_exit = fetch_option_chain(kite=kite)
    except Exception:
        kite, spot, oc_exit = None, 0.0, None

    logger.info(f"3:25 PM force exit: {trade.action} {trade.strike}")
    _close_directional_slice(trade, kite, spot, oc_exit, trade.lots,
                              tag="FORCE_EXIT_325PM", time_label="3:25 PM", remaining_qty=0)


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


def _enter_strangle(kite, signal, qty: int, spot: float, oc) -> bool:
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


def _check_sl_hit(kite, trade: TradeState, spot: float, oc=None) -> bool:
    """Auto-exit if spot crossed the SL level. Returns True if trade was closed."""
    global strangle_legs

    hit = False
    if trade.action == "CALL_SELL" and spot >= trade.sl_spot_level:
        hit = True
    elif trade.action == "PUT_SELL" and spot <= trade.sl_spot_level:
        hit = True
    elif trade.action == "STRANGLE":
        if spot >= trade.sl_spot_level:
            hit = True
        elif trade.sl_put and spot <= trade.sl_put:
            hit = True

    if not hit:
        return False

    logger.warning(
        f"SL HIT: {trade.action} {trade.strike}  spot={spot:.1f}  SL={trade.sl_spot_level:.1f}"
    )
    opt_type       = "CE" if trade.action == "CALL_SELL" else "PE"
    exit_ltp       = None
    hedge_exit_ltp = None
    if trade.lots > 0:
        try:
            if trade.action == "STRANGLE" and strangle_legs:
                if strangle_legs.ce_active:
                    ep = _buy_price(oc, "CE", strangle_legs.ce_strike) if oc else None
                    place_buy_order(kite, strangle_legs.ce_symbol, trade.lots, price=ep)
                    if strangle_legs.hedge_ce_symbol:
                        hp = _sell_price(oc, "CE", strangle_legs.hedge_ce_strike) if oc else None
                        place_sell_order(kite, strangle_legs.hedge_ce_symbol, trade.lots, price=hp)
                if strangle_legs.pe_active:
                    ep = _buy_price(oc, "PE", strangle_legs.pe_strike) if oc else None
                    place_buy_order(kite, strangle_legs.pe_symbol, trade.lots, price=ep)
                    if strangle_legs.hedge_pe_symbol:
                        hp = _sell_price(oc, "PE", strangle_legs.hedge_pe_strike) if oc else None
                        place_sell_order(kite, strangle_legs.hedge_pe_symbol, trade.lots, price=hp)
            else:
                if oc:
                    sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
                    exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                ep = _buy_price(oc, opt_type, trade.strike) if oc else None
                place_buy_order(kite, trade.symbol, trade.lots, price=ep)
                if trade.hedge_symbol and trade.hedge_strike:
                    if oc:
                        hsd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.hedge_strike)
                        hedge_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                    hp = _sell_price(oc, opt_type, trade.hedge_strike) if oc else None
                    place_sell_order(kite, trade.hedge_symbol, trade.lots, price=hp)
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
        if strangle_legs.ce_active and qty > 0:
            ep = _buy_price(oc, "CE", strangle_legs.ce_strike)
            r = place_buy_order(kite, strangle_legs.ce_symbol, qty, price=ep)
            if not r.success:
                send_error_alert(f"CE leg exit failed: {r.error}")
            if strangle_legs.hedge_ce_symbol:
                hp = _sell_price(oc, "CE", strangle_legs.hedge_ce_strike)
                hr = place_sell_order(kite, strangle_legs.hedge_ce_symbol, qty, price=hp)
                if not hr.success:
                    send_error_alert(f"CE hedge unwind failed: {hr.error}")
        strangle_legs.ce_active = False
        logger.info(f"CE leg {strangle_legs.ce_strike} closed. PE {strangle_legs.pe_strike} running.")

    elif decision.action == EXIT_PE_LEG and strangle_legs:
        if strangle_legs.pe_active and qty > 0:
            ep = _buy_price(oc, "PE", strangle_legs.pe_strike)
            r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=ep)
            if not r.success:
                send_error_alert(f"PE leg exit failed: {r.error}")
            if strangle_legs.hedge_pe_symbol:
                hp = _sell_price(oc, "PE", strangle_legs.hedge_pe_strike)
                hr = place_sell_order(kite, strangle_legs.hedge_pe_symbol, qty, price=hp)
                if not hr.success:
                    send_error_alert(f"PE hedge unwind failed: {hr.error}")
        strangle_legs.pe_active = False
        logger.info(f"PE leg {strangle_legs.pe_strike} closed. CE {strangle_legs.ce_strike} running.")

    elif decision.action == PARTIAL_PROFIT_LOCK:
        lock_qty = trade.lots - NIFTY_LOT_SIZE   # exit all-but-1-lot
        exit_ltp = None
        if lock_qty > 0:
            sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
            exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
            r = place_buy_order(kite, trade.symbol, lock_qty, price=_buy_price(oc, opt_type, trade.strike))
            if not r.success:
                send_error_alert(f"Partial profit lock order failed: {r.error}")
            else:
                locked_pnl = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * lock_qty
                monitor.trade.lots             = NIFTY_LOT_SIZE
                monitor.trade.partial_profit_locked = True
                locked_s   = f"₹{exit_ltp:.2f}" if exit_ltp else "~market"
                _post(
                    f"🔒 *PARTIAL PROFIT LOCKED*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Exited *{lock_qty} shares* of {trade.action.replace('_',' ')} {trade.strike}\n"
                    f"Entry ₹{trade.entry_premium:.2f} → Exit *{locked_s}*\n"
                    f"💰 Locked: *{_fmt_pnl(locked_pnl)}*\n"
                    f"\n"
                    f"📌 *1 lot ({NIFTY_LOT_SIZE} shares) still running free*\n"
                    f"Will exit on 2 consecutive opposing signals or 2:55 PM.\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                logger.info(f"Partial lock done: {lock_qty} exited @ {exit_ltp}, {NIFTY_LOT_SIZE} remaining")
        return   # skip send_management_alert at the bottom

    elif decision.action == EXIT_FULL:
        exit_ltp       = None
        hedge_exit_ltp = None
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
                sd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.strike)
                exit_ltp = sd.ltp if sd and sd.ltp > 0 else None
                r = place_buy_order(kite, trade.symbol, qty, price=_buy_price(oc, opt_type, trade.strike))
                if not r.success: send_error_alert(f"Exit order failed: {r.error}")
                if trade.hedge_symbol and trade.hedge_strike:
                    hsd = (oc.call_data if opt_type == "CE" else oc.put_data).get(trade.hedge_strike)
                    hedge_exit_ltp = hsd.ltp if hsd and hsd.ltp > 0 else None
                    place_sell_order(kite, trade.hedge_symbol, qty, price=_sell_price(oc, opt_type, trade.hedge_strike))
        if trade.action != "STRANGLE":
            main_pnl  = (trade.entry_premium - (exit_ltp or trade.entry_premium)) * qty
            hedge_pnl = (
                (hedge_exit_ltp - trade.hedge_entry_premium) * qty
                if hedge_exit_ltp is not None and trade.hedge_entry_premium is not None else 0.0
            )
            _record_trade_exit(trade, exit_ltp, hedge_exit_ltp, main_pnl, hedge_pnl, main_pnl + hedge_pnl, spot, "SIGNAL_EXIT")
            # Block same-direction re-entry for the rest of the session
            global _signal_exit_blocked
            _signal_exit_blocked = trade.action
            logger.info(f"Cooldown set: {trade.action} blocked for rest of session after SIGNAL_EXIT")
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
                    place_sell_order(kite, trade.hedge_symbol, qty, price=_sell_price(oc, opt_type, trade.hedge_strike))
        monitor.clear_trade()
        strangle_legs = None

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
    global strangle_legs
    if not _is_market_open():
        logger.info("Market closed — skipping.")
        return

    logger.info("═" * 55)
    logger.info(f"Scan @ {datetime.now().strftime('%H:%M:%S')}")

    try:
        kite = _get_kite()

        try:
            _check_total_mtm()
        except Exception as e:
            logger.exception(f"Total MTM check failed: {e}")

        df   = fetch_nifty_candles(kite)
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
            trade = monitor.trade

            # Auto-exit if SL crossed (before position manager to avoid double processing)
            if _check_sl_hit(kite, trade, spot, oc=oc):
                return

            # Run monitor first — updates reversal_candle_count and other counters
            mon_result = monitor.check(oc=oc, rsi=rsi_result, spot=spot)

            # Current LTP of sold leg (for partial profit lock P&L calculation)
            opt_type_live = "CE" if trade.action == "CALL_SELL" else "PE"
            live_sd       = (oc.call_data if opt_type_live == "CE" else oc.put_data).get(trade.strike)
            current_ltp   = live_sd.ltp if live_sd and live_sd.ltp > 0 else None

            # Position manager: should we adjust/exit/reverse?
            pm_decision = evaluate_position(
                trade=trade,
                tl=tl_result,
                rsi=rsi_result,
                opt=opt_signal,
                strangle_legs=strangle_legs,
                reversal_candle_count=monitor.reversal_candle_count,
                clean_after_hedge_count=monitor.clean_after_hedge_count,
                hedge_active=monitor.hedge_active,
                current_ltp=current_ltp,
            )

            if pm_decision.action != HOLD:
                _handle_management_decision(
                    pm_decision, trade, spot,
                    tl_result, rsi_result, opt_signal, oc, df,
                )
            else:
                if mon_result.has_warning:
                    sl_warnings = [w for w in mon_result.warnings if w.category == "SL_PROXIMITY"]
                    if sl_warnings:
                        send_trade_warning(trade, mon_result, spot)
                final_sig = combine_signals(
                    tl=tl_result, rsi=rsi_result, opt=opt_signal,
                    spot_price=spot, expiry=oc.weekly_expiry_date,
                )
                send_live_pnl_update(trade, spot, oc, signal=final_sig, strangle_legs=strangle_legs)

            # If strangle has only one leg remaining, also check for entry of opposite
            if (monitor.trade and monitor.trade.action == "STRANGLE"
                    and strangle_legs and strangle_legs.remaining_leg is not None):
                logger.info(f"Strangle single leg running: {strangle_legs.remaining_leg}")

        # ── New entry (no active trade) ───────────────────────────
        elif not _total_loss_hit and _entry_allowed():
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            logger.info(f"Signal: {final.action}  score={final.score}/3  lots={final.lots}")

            if final.action == _signal_exit_blocked:
                logger.info(f"Same-direction cooldown: {final.action} blocked after SIGNAL_EXIT — observing only")
                send_signal(final, observation=True)
            else:
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
                        ))
                        _save_state()
                    else:
                        send_error_alert(f"Entry order failed: {sell_res.error}")

                elif final.action == "STRANGLE" and _strangle_entry_allowed():
                    qty = final.strangle_lots * NIFTY_LOT_SIZE
                    _enter_strangle(kite, final, qty, spot, oc)
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
    logger.info("Strangles held overnight | Directionals: split exit 2:55 PM / 3:25 PM (single lot exits fully at 2:55 PM)")

    start_command_listener()
    _restore_positions_from_kite()
    run_scan()

    for minute in [":00", ":15", ":30", ":45"]:
        schedule.every().hour.at(minute).do(run_scan)

    schedule.every().day.at("14:55").do(force_exit_all)
    schedule.every().day.at("15:25").do(force_exit_remaining)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
