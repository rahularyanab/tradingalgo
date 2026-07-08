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
    NIFTY_LOT_SIZE, MANUAL_POSITION_MAX_LOSS,
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
_manual_loss_hit: bool = False    # True once the manual-position guard has squared off for the day
_manual_loss_reset_date = None   # last date _manual_loss_hit was reset — detects day rollover


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


def _parse_nifty_option_symbol(sym: str):
    """Parse Zerodha weekly NFO symbol e.g. NIFTY2670724100CE → (strike, expiry, opt_type)."""
    body = sym[5:]  # strip 'NIFTY'
    yy   = body[0:2]
    mm   = body[2]
    dd   = body[3:5]
    opt_type = body[-2:]   # 'CE' or 'PE'
    strike_str = body[5:-2]
    MONTHS = {'1':'Jan','2':'Feb','3':'Mar','4':'Apr','5':'May','6':'Jun',
               '7':'Jul','8':'Aug','9':'Sep','O':'Oct','N':'Nov','D':'Dec'}
    month_name = MONTHS.get(mm, 'Jul')
    return int(strike_str), f"{dd}-{month_name}-20{yy}", opt_type


def _restore_positions_from_kite():
    """On startup, restore monitor trade state from existing Kite positions (nearest weekly expiry only)."""
    from datetime import date
    import re

    MONTH_MAP = {'1':1,'2':2,'3':3,'4':4,'5':5,'6':6,
                 '7':7,'8':8,'9':9,'O':10,'N':11,'D':12}

    def expiry_date(sym: str) -> date:
        body = sym[5:]
        yy, mm_char, dd = int(body[0:2]), body[2], int(body[3:5])
        return date(2000 + yy, MONTH_MAP.get(mm_char, 7), dd)

    try:
        kite = _get_kite()
        all_pos = kite.positions()['net']
        nfty = [
            p for p in all_pos
            if p.get('exchange') == 'NFO'
            and re.match(r'^NIFTY\d{2}[0-9OND]\d{2}\d{4,5}(CE|PE)$', p.get('tradingsymbol', ''))
            and p['quantity'] != 0
        ]
        if not nfty:
            return

        # Find the nearest expiry among all NIFTY positions
        nearest = min(nfty, key=lambda p: expiry_date(p['tradingsymbol']))
        nearest_exp = expiry_date(nearest['tradingsymbol'])

        # Only use positions from that nearest expiry
        week_pos    = [p for p in nfty if expiry_date(p['tradingsymbol']) == nearest_exp]
        short_legs  = [p for p in week_pos if p['quantity'] < 0]
        long_legs   = [p for p in week_pos if p['quantity'] > 0]

        if not short_legs:
            return

        ce_short = next((p for p in short_legs if p['tradingsymbol'].endswith('CE')), None)
        pe_short = next((p for p in short_legs if p['tradingsymbol'].endswith('PE')), None)

        if ce_short and pe_short:
            # Strangle
            ce_strike, expiry, _ = _parse_nifty_option_symbol(ce_short['tradingsymbol'])
            pe_strike, _,      _ = _parse_nifty_option_symbol(pe_short['tradingsymbol'])
            qty = abs(ce_short['quantity'])
            monitor.set_trade(TradeState(
                action="STRANGLE",
                strike=ce_strike,
                symbol=f"{ce_short['tradingsymbol']} + {pe_short['tradingsymbol']}",
                entry_time=datetime.now(),
                entry_premium=abs(ce_short['average_price']) + abs(pe_short['average_price']),
                entry_spot=0,
                sl_spot_level=float(ce_strike),
                sl_put=float(pe_strike),
                expiry=expiry,
                lots=qty,
            ))
        else:
            sp = ce_short or pe_short
            strike, expiry, opt_type = _parse_nifty_option_symbol(sp['tradingsymbol'])
            action = 'CALL_SELL' if opt_type == 'CE' else 'PUT_SELL'
            # Find matching hedge (same expiry, opposite direction)
            hedge_p             = long_legs[0] if long_legs else None
            hedge_sym           = hedge_p['tradingsymbol'] if hedge_p else None
            hedge_strike        = _parse_nifty_option_symbol(hedge_sym)[0] if hedge_sym else None
            hedge_entry_premium = abs(hedge_p['average_price']) if hedge_p else None
            monitor.set_trade(TradeState(
                action=action,
                strike=strike,
                symbol=sp['tradingsymbol'],
                entry_time=datetime.now(),
                entry_premium=abs(sp['average_price']),
                entry_spot=0,
                sl_spot_level=float(strike),
                expiry=expiry,
                lots=abs(sp['quantity']),
                hedge_symbol=hedge_sym,
                hedge_strike=hedge_strike,
                hedge_entry_premium=hedge_entry_premium,
            ))

        t = monitor.trade
        if t:
            logger.info(f"Restored {t.action} {t.strike}  qty={t.lots}  hedge={t.hedge_symbol}")
            _post(
                f"♻️ *Position Restored on Restart*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Trade: *{t.action.replace('_',' ')} {t.strike}*  qty={t.lots}\n"
                f"Avg entry: ₹{t.entry_premium:.2f}  |  Expiry: {t.expiry}\n"
                f"SL spot: {t.sl_spot_level:.0f}  _(defaults to sold strike)_\n"
                f"_Monitoring resumed._"
            )
    except Exception as e:
        logger.error(f"Position restore failed: {e}")


def _check_manual_positions():
    """
    Square off any open position the algo itself didn't place (e.g. trades
    entered manually in the same Zerodha account) once their combined P&L
    breaches -MANUAL_POSITION_MAX_LOSS. The algo's own trade — main leg and
    hedge, or both strangle legs — is always excluded by tradingsymbol.
    """
    global _manual_loss_hit, _manual_loss_reset_date

    today = datetime.now().date()
    if _manual_loss_reset_date != today:
        _manual_loss_hit        = False
        _manual_loss_reset_date = today

    if _manual_loss_hit:
        return

    try:
        kite    = _get_kite()
        all_pos = kite.positions()['net']
    except Exception as e:
        logger.warning(f"Manual position check: positions() failed: {e}")
        return

    algo_symbols = set()
    if monitor.trade:
        if monitor.trade.action == "STRANGLE":
            algo_symbols.update(monitor.trade.symbol.split(" + "))
        else:
            algo_symbols.add(monitor.trade.symbol)
        if monitor.trade.hedge_symbol:
            algo_symbols.add(monitor.trade.hedge_symbol)

    manual_positions = [
        p for p in all_pos
        if p.get('quantity', 0) != 0 and p.get('tradingsymbol') not in algo_symbols
    ]
    if not manual_positions:
        return

    total_pnl = sum(p.get('pnl', 0) for p in manual_positions)
    logger.info(f"Manual positions MTM: {_fmt_pnl(total_pnl)}  ({len(manual_positions)} position(s))")

    if total_pnl > -MANUAL_POSITION_MAX_LOSS:
        return

    logger.warning(
        f"Manual position loss breach: {_fmt_pnl(total_pnl)}  "
        f"≤  -₹{MANUAL_POSITION_MAX_LOSS:,} — squaring off"
    )
    closed = []
    for p in manual_positions:
        symbol   = p['tradingsymbol']
        qty      = p['quantity']
        exchange = p.get('exchange') or 'NFO'
        product  = p.get('product') or 'NRML'
        ltp      = p.get('last_price') or None
        result = square_off_position(kite, symbol, qty, exchange=exchange, product=product, ltp=ltp)
        if result.success:
            closed.append((symbol, abs(qty), p.get('pnl', 0)))
        else:
            send_error_alert(f"Manual position square-off failed for {symbol}: {result.error}")

    _manual_loss_hit = True
    lines = [
        f"🚨 *MANUAL POSITIONS SQUARED OFF*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Combined loss {_fmt_pnl(total_pnl)} breached -₹{MANUAL_POSITION_MAX_LOSS:,} threshold.",
        f"",
    ]
    for sym, qty, pnl in closed:
        lines.append(f"   `{sym}`  qty={qty}   {_fmt_pnl(pnl)}")
    lines += [
        f"",
        f"_Algo's own trade left untouched. No more manual-position checks today._",
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
    )


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
                if strangle_legs.pe_active:
                    ep = _buy_price(oc, "PE", strangle_legs.pe_strike) if oc else None
                    place_buy_order(kite, strangle_legs.pe_symbol, trade.lots, price=ep)
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
        strangle_legs.ce_active = False
        logger.info(f"CE leg {strangle_legs.ce_strike} closed. PE {strangle_legs.pe_strike} running.")

    elif decision.action == EXIT_PE_LEG and strangle_legs:
        if strangle_legs.pe_active and qty > 0:
            ep = _buy_price(oc, "PE", strangle_legs.pe_strike)
            r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=ep)
            if not r.success:
                send_error_alert(f"PE leg exit failed: {r.error}")
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
                if strangle_legs.pe_active:
                    r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=_buy_price(oc, "PE", strangle_legs.pe_strike))
                    if not r.success: send_error_alert(f"PE exit failed: {r.error}")
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
                if strangle_legs.pe_active:
                    r = place_buy_order(kite, strangle_legs.pe_symbol, qty, price=_buy_price(oc, "PE", strangle_legs.pe_strike))
                    if not r.success: send_error_alert(f"PE exit failed: {r.error}")
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
                ce_res  = place_sell_order(kite, new_signal.call_symbol, new_qty, price=new_signal.call_premium)
                pe_res  = place_sell_order(kite, new_signal.put_symbol,  new_qty, price=new_signal.put_premium)
                if ce_res.success and pe_res.success:
                    monitor.set_trade(TradeState(
                        action="STRANGLE",
                        strike=new_signal.call_strike,
                        symbol=f"{new_signal.call_symbol} + {new_signal.put_symbol}",
                        entry_time=datetime.now(),
                        entry_premium=(new_signal.call_premium or 0) + (new_signal.put_premium or 0),
                        entry_spot=spot,
                        sl_spot_level=new_signal.call_sl or spot,
                        sl_put=new_signal.put_sl,
                        expiry=new_signal.expiry,
                        lots=new_qty,
                    ))
                    _register_strangle(new_signal, spot, oc)
                else:
                    errors = [r.error for r in (ce_res, pe_res) if not r.success]
                    send_error_alert(f"Strangle re-entry failed: {'; '.join(errors)}")
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
            _check_manual_positions()
        except Exception as e:
            logger.exception(f"Manual position check failed: {e}")

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
        elif _entry_allowed():
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
                    else:
                        send_error_alert(f"Entry order failed: {sell_res.error}")

                elif final.action == "STRANGLE" and _strangle_entry_allowed():
                    qty    = final.strangle_lots * NIFTY_LOT_SIZE
                    ce_res = place_sell_order(kite, final.call_symbol, qty, price=final.call_premium)
                    pe_res = place_sell_order(kite, final.put_symbol,  qty, price=final.put_premium)
                    if ce_res.success and pe_res.success:
                        monitor.set_trade(TradeState(
                            action="STRANGLE",
                            strike=final.call_strike,
                            symbol=f"{final.call_symbol} + {final.put_symbol}",
                            entry_time=datetime.now(),
                            entry_premium=(final.call_premium or 0) + (final.put_premium or 0),
                            entry_spot=spot,
                            sl_spot_level=final.call_sl or spot,
                            sl_put=final.put_sl,
                            expiry=final.expiry,
                            lots=qty,
                        ))
                        _register_strangle(final, spot, oc)
                    else:
                        errors = [r.error for r in (ce_res, pe_res) if not r.success]
                        send_error_alert(f"Strangle entry failed: {'; '.join(errors)}")
        else:
            # Outside entry window (before 10:00 or after force-exit): observe only
            final = combine_signals(
                tl=tl_result, rsi=rsi_result, opt=opt_signal,
                spot_price=spot, expiry=oc.weekly_expiry_date,
            )
            send_signal(final, observation=True)

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
