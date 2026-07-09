"""
Sends formatted Nifty option selling signals and trade updates to Telegram.

Design principles:
  - Every message has a SIGNAL DASHBOARD table so nothing needs comparison
    with the previous message.
  - P&L updates end with a one-line VERDICT: hold / caution / exit.
  - OI table uses ▲/▼ arrows to show COI direction at a glance.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from signals.combiner import FinalSignal
from signals.trade_monitor import MonitorResult, TradeState

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

_PROXY   = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
_PROXIES = {"https": _PROXY, "http": _PROXY} if _PROXY else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_str() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%H:%M IST"), now.strftime("%d %b %Y")


def _post(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set in .env")
        return False
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
            proxies=_PROXIES,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def _fmt_oi(oi: float) -> str:
    if oi >= 100_000:
        return f"{oi/100_000:.1f}L"
    if oi >= 1_000:
        return f"{oi/1_000:.0f}k"
    return str(int(oi))


def _fmt_coi(coi: float) -> str:
    if coi == 0:
        return "  —   "
    arrow = "▲" if coi > 0 else "▼"
    if abs(coi) >= 100_000:
        return f"{arrow}{abs(coi)/100_000:.1f}L"
    if abs(coi) >= 1_000:
        return f"{arrow}{abs(coi)/1_000:.0f}k"
    return f"{arrow}{abs(int(coi))}"


def _signal_dashboard(sig: FinalSignal, trade_action: str = "") -> str:
    """
    Three-row signal table. trade_action="" when no trade open.
    Returns multi-line string (no surrounding block — caller wraps it).
    """
    # ── RSI row ──────────────────────────────────────────────────
    if sig.bearish_divergence:
        price_arrow = "↑" if sig.spot_price >= sig.price_prev_pivot else "↓"
        rsi_icon   = "✅"
        rsi_label  = "RSI Diverge"
        rsi_detail = f"Bearish  RSI {sig.rsi_prev:.1f}→{sig.rsi_current:.1f} | price {price_arrow}{sig.price_prev_pivot:.0f}→{sig.spot_price:.0f}"
        rsi_vs     = "🟢 ALIGNED" if trade_action == "CALL_SELL" else ("🔴 AGAINST" if trade_action == "PUT_SELL" else "")
    elif sig.bullish_divergence:
        price_arrow = "↓" if sig.spot_price <= sig.price_prev_pivot else "↑"
        rsi_icon   = "✅"
        rsi_label  = "RSI Diverge"
        rsi_detail = f"Bullish  RSI {sig.rsi_prev:.1f}→{sig.rsi_current:.1f} | price {price_arrow}{sig.price_prev_pivot:.0f}→{sig.spot_price:.0f}"
        rsi_vs     = "🟢 ALIGNED" if trade_action == "PUT_SELL" else ("🔴 AGAINST" if trade_action == "CALL_SELL" else "")
    else:
        rsi_icon   = "❌"
        rsi_label  = "RSI Diverge"
        rsi_detail = f"None     RSI {sig.rsi_current:.1f}"
        rsi_vs     = "—"

    # ── Trendline row ─────────────────────────────────────────────
    if sig.at_resistance and sig.resistance_level:
        tl_icon   = "✅"
        tl_label  = "Resistance "
        tl_detail = f"At {sig.resistance_level:.0f}  (price near from below)"
        tl_vs     = "🟢 ALIGNED" if trade_action == "CALL_SELL" else ("🔴 AGAINST" if trade_action == "PUT_SELL" else "")
    elif sig.at_support and sig.support_level:
        tl_icon   = "✅"
        tl_label  = "Support    "
        tl_detail = f"At {sig.support_level:.0f}  (price near from above)"
        tl_vs     = "🟢 ALIGNED" if trade_action == "PUT_SELL" else ("🔴 AGAINST" if trade_action == "CALL_SELL" else "")
    else:
        res_s = f"{sig.resistance_level:.0f}" if sig.resistance_level else "—"
        sup_s = f"{sig.support_level:.0f}"    if sig.support_level    else "—"
        tl_icon   = "❌"
        tl_label  = "Trendline  "
        tl_detail = f"None     Res {res_s} | Sup {sup_s}"
        tl_vs     = "—"

    # ── OI / Writing row ──────────────────────────────────────────
    if sig.call_bearish_pcr:
        oi_icon   = "✅"
        oi_label  = "OI Signal  "
        oi_detail = f"Bearish  PCR {sig.pcr} (<0.8) | call wall {sig.call_wall}"
        oi_vs     = "🟢 ALIGNED" if trade_action == "CALL_SELL" else ("🔴 AGAINST" if trade_action == "PUT_SELL" else "")
    elif sig.call_writing_bearish:
        oi_icon   = "✅"
        oi_label  = "OI Signal  "
        oi_detail = f"Call writing  Fresh COI at ATM strikes | wall {sig.call_wall}"
        oi_vs     = "🟢 ALIGNED" if trade_action == "CALL_SELL" else ("🔴 AGAINST" if trade_action == "PUT_SELL" else "")
    elif sig.put_bullish_pcr:
        oi_icon   = "✅"
        oi_label  = "OI Signal  "
        oi_detail = f"Bullish  PCR {sig.pcr} (>1.2) | put wall {sig.put_wall}"
        oi_vs     = "🟢 ALIGNED" if trade_action == "PUT_SELL" else ("🔴 AGAINST" if trade_action == "CALL_SELL" else "")
    elif sig.put_writing_bullish:
        oi_icon   = "✅"
        oi_label  = "OI Signal  "
        oi_detail = f"Put writing  Fresh COI at ATM strikes | wall {sig.put_wall}"
        oi_vs     = "🟢 ALIGNED" if trade_action == "PUT_SELL" else ("🔴 AGAINST" if trade_action == "CALL_SELL" else "")
    else:
        oi_icon   = "❌"
        oi_label  = "OI Signal  "
        oi_detail = f"None     PCR {sig.pcr} (neutral 0.8–1.2)"
        oi_vs     = "—"

    if trade_action:
        rows = [
            f"{rsi_icon} `{rsi_label}` {rsi_detail}",
            f"   ↳ vs trade: *{rsi_vs}*",
            f"{tl_icon} `{tl_label}` {tl_detail}",
            f"   ↳ vs trade: *{tl_vs}*",
            f"{oi_icon} `{oi_label}` {oi_detail}",
            f"   ↳ vs trade: *{oi_vs}*",
        ]
    else:
        rows = [
            f"{rsi_icon} `{rsi_label}` {rsi_detail}",
            f"{tl_icon} `{tl_label}` {tl_detail}",
            f"{oi_icon} `{oi_label}` {oi_detail}",
        ]

    return "\n".join(rows)


def _oi_table(sig: FinalSignal) -> str:
    """Compact OI table for top calls + puts."""
    call_rows = [
        f"   🔴 `{s.strike}CE`  OI {_fmt_oi(s.oi)}  COI {_fmt_coi(s.change_oi)}  ₹{s.ltp}"
        for s in sig.top_2_calls
    ] or ["   —"]
    put_rows = [
        f"   🟢 `{s.strike}PE`  OI {_fmt_oi(s.oi)}  COI {_fmt_coi(s.change_oi)}  ₹{s.ltp}"
        for s in sig.top_2_puts
    ] or ["   —"]
    return (
        "*OI SNAPSHOT*\n"
        + "\n".join(call_rows)
        + "\n"
        + "\n".join(put_rows)
        + f"\n   Max Pain `{sig.max_pain}`  |  ΔPCR `{sig.change_pcr}`"
    )


def _trade_verdict(trade_action: str, sig: FinalSignal) -> str:
    """Single-line verdict based on whether current signals support the open trade."""
    if trade_action == "CALL_SELL":
        aligned = sum([
            sig.bearish_divergence,
            sig.call_writing_bearish or sig.call_bearish_pcr,
            sig.at_resistance,
        ])

        # 3/3 aligned → never show CAUTION regardless of minor counter-signals
        if aligned == 3:
            return (
                f"✅ *HOLD WITH CONFIDENCE — 3/3 bearish signals active.*\n"
                f"_Trade thesis intact. Let premium decay._"
            )

        against = []
        if sig.bullish_divergence:
            against.append("bullish RSI divergence forming")
        if sig.put_bullish_pcr:
            against.append("PCR turning bullish")
        # Only flag put writing when PCR has also turned bullish — raw writing alone
        # can coexist with a bearish PCR (defensive hedging, not a bullish reversal)
        if sig.put_writing_bullish and sig.put_bullish_pcr:
            against.append("put writing + PCR turning bullish")

        if against:
            return (
                f"⚠️ *CAUTION — {against[0]}.*\n"
                f"_Signals shifting. Tighten SL or consider partial cover._"
            )
        if aligned >= 2:
            return (
                f"✅ *HOLD WITH CONFIDENCE — {aligned}/3 bearish signals active.*\n"
                f"_Trade thesis intact. Let premium decay._"
            )
        if aligned == 1:
            return (
                f"🟡 *HOLD — 1 bearish signal remains.*\n"
                f"_No reversal yet but conviction lower. Watch SL closely._"
            )
        return (
            f"🟡 *HOLD — Signals neutral.*\n"
            f"_No exit trigger. Trade within risk parameters._"
        )

    if trade_action == "PUT_SELL":
        aligned = sum([
            sig.bullish_divergence,
            sig.put_writing_bullish or sig.put_bullish_pcr,
            sig.at_support,
        ])

        if aligned == 3:
            return (
                f"✅ *HOLD WITH CONFIDENCE — 3/3 bullish signals active.*\n"
                f"_Trade thesis intact. Let premium decay._"
            )

        against = []
        if sig.bearish_divergence:
            against.append("bearish RSI divergence forming")
        if sig.call_bearish_pcr:
            against.append("PCR turning bearish")
        if sig.call_writing_bearish and sig.call_bearish_pcr:
            against.append("call writing + PCR turning bearish")

        if against:
            return (
                f"⚠️ *CAUTION — {against[0]}.*\n"
                f"_Signals shifting. Tighten SL or consider partial cover._"
            )
        if aligned >= 2:
            return (
                f"✅ *HOLD WITH CONFIDENCE — {aligned}/3 bullish signals active.*\n"
                f"_Trade thesis intact. Let premium decay._"
            )
        if aligned == 1:
            return (
                f"🟡 *HOLD — 1 bullish signal remains.*\n"
                f"_Watch SL closely._"
            )
        return (
            f"🟡 *HOLD — Signals neutral.*\n"
            f"_No exit trigger. Trade within risk parameters._"
        )

    if trade_action == "STRANGLE":
        directional = []
        if sig.bearish_divergence:
            directional.append("bearish RSI divergence — CE leg at risk")
        if sig.bullish_divergence:
            directional.append("bullish RSI divergence — PE leg at risk")
        if sig.call_bearish_pcr:
            directional.append("PCR bearish (<0.8)")
        if sig.put_bullish_pcr:
            directional.append("PCR bullish (>1.2)")

        if directional:
            return (
                f"⚠️ *CAUTION — {directional[0]}.*\n"
                f"_Market losing neutral character. One leg may need exit._"
            )
        return f"✅ *HOLD STRANGLE — Range bound. Both legs safe.*"

    return "🟡 *Monitoring.*"


# ── Signal message (no trade open) ───────────────────────────────────────────

def send_signal(signal: FinalSignal, observation: bool = False) -> bool:
    time_s, date_s = _now_str()
    obs_tag = "👁️ *OBSERVING*" if observation else "📊 *NIFTY SCAN*"
    header  = f"{obs_tag}  |  {time_s}  |  {date_s}"
    obs_footer = "_No trade placed. Entry window opens at 10:00 AM._" if observation else "_No trade entry. Monitoring next candle._"

    if signal.action == "NO_SIGNAL":
        text = "\n".join([
            header,
            "━━━━━━━━━━━━━━━━━━━━",
            f"Spot `{signal.spot_price:.0f}`  PCR `{signal.pcr}`  RSI `{signal.rsi_current:.1f}`",
            "",
            f"*SIGNAL DASHBOARD  ({signal.score}/3 conditions met)*",
            _signal_dashboard(signal),
            "",
            _oi_table(signal),
            "━━━━━━━━━━━━━━━━━━━━",
            obs_footer,
        ])
        return _post(text)

    if signal.action == "STRANGLE":
        target_pct = int(signal.target_decay * 100)
        if observation:
            trade_lines = [
                "⏳ *SIGNAL FORMING — no trade yet (entry at 10:00)*",
                f"Would sell: `{signal.call_strike} CE` @ ₹{signal.call_premium}  +  `{signal.put_strike} PE` @ ₹{signal.put_premium}",
            ]
            footer = "_Watching one more candle. Bot enters at 10:00 if signal holds._"
        else:
            trade_lines = [
                "⚡ *SHORT STRANGLE — NON-DIRECTIONAL*",
                f"🔴 *SELL CALL* `{signal.call_strike} CE` @ *₹{signal.call_premium}*  ({signal.strangle_lots} lots)",
                f"🟢 *SELL PUT * `{signal.put_strike} PE` @ *₹{signal.put_premium}*  ({signal.strangle_lots} lots)",
                *(
                    [f"Net premium: *₹{signal.net_premium}* (after hedges)"]
                    if signal.net_premium else []
                ),
                "",
                "*WHY NON-DIRECTIONAL*",
                *[f"   ✅ {r}" for r in signal.reasons],
                "",
                f"*⚠️ STOP LOSS*",
                f"   Call: spot > `{signal.call_sl:.0f}` → exit both",
                f"   Put : spot < `{signal.put_sl:.0f}` → exit both",
                f"*🎯 Target*: {target_pct}% premium decay",
            ]
            footer = ""
        text = "\n".join(filter(None, [
            header,
            "━━━━━━━━━━━━━━━━━━━━",
            f"Spot `{signal.spot_price:.0f}`  PCR `{signal.pcr}`  Expiry {signal.expiry}",
            "",
            *trade_lines,
            "━━━━━━━━━━━━━━━━━━━━",
            footer,
        ]))
        return _post(text)

    # ── Directional ───────────────────────────────────────────────
    is_call = signal.action == "CALL_SELL"
    direction_header = "🔴 *CALL SELL SIGNAL*" if is_call else "🟢 *PUT SELL SIGNAL*"
    opt_type         = "CE" if is_call else "PE"
    strength_emoji   = {"STRONG": "💪", "MODERATE": "👍"}.get(signal.strength, "")
    target_pct       = int(signal.target_decay * 100)

    hedge_strike = signal.hedge_call_strike if is_call else signal.hedge_put_strike
    hedge_ltp    = signal.hedge_call_ltp    if is_call else signal.hedge_put_ltp

    if observation:
        action_lines = [
            f"⏳ *SIGNAL FORMING — no trade yet (entry at 10:00)*",
            f"Would sell: `NIFTY {signal.strike} {opt_type}` @ ₹{signal.premium}  SL spot {'>' if is_call else '<'} `{signal.sl_spot_level:.0f}`",
        ]
        trade_footer = f"_Watching one more candle. Bot enters at 10:00 if signal holds ({signal.score}/3)._"
    else:
        action_lines = [
            "─────────────────────",
            f"🔴 *SELL* `NIFTY {signal.strike} {opt_type}` @ *₹{signal.premium}*  ({signal.lots} lots)",
            *(
                [f"🟩 *BUY hedge* `NIFTY {hedge_strike} {opt_type}` @ *₹{hedge_ltp}*  _(margin)_"]
                if hedge_strike else []
            ),
            *(
                [f"Net premium: *₹{signal.net_premium}*"]
                if signal.net_premium else []
            ),
            f"Symbol: `{signal.symbol}`",
        ]
        trade_footer = "_Educational purposes only. Trade at your own risk._"

    text = "\n".join([
        header,
        "━━━━━━━━━━━━━━━━━━━━",
        direction_header,
        f"Spot `{signal.spot_price:.0f}`  Expiry {signal.expiry}",
        "",
        f"*SIGNAL DASHBOARD  ({signal.score}/3)*",
        _signal_dashboard(signal),
        "",
        f"{strength_emoji} *Strength: {signal.strength}*",
        "",
        *action_lines,
        "",
        _oi_table(signal),
        "",
        f"*⚠️ Stop Loss*: spot {'>' if is_call else '<'} `{signal.sl_spot_level:.0f}`",
        f"*🎯 Target*: {target_pct}% premium decay",
        "━━━━━━━━━━━━━━━━━━━━",
        trade_footer,
    ])
    return _post(text)


# ── Paper trade entry ─────────────────────────────────────────────────────────

def send_paper_entry(pos_summary: dict, signal_reasons: list) -> bool:
    time_s, date_s = _now_str()
    action = pos_summary["action"]

    if action == "STRANGLE":
        legs_text = "\n".join(
            f"   {'🔴' if l['type']=='CE' else '🟢'} `{l['strike']} {l['type']}` "
            f"@ *₹{l['entry']}*  ({l['lots']} lots)"
            for l in pos_summary["legs"]
        )
        sl_text = (
            f"   Call: spot > `{pos_summary['sl_call']:.0f}`\n"
            f"   Put : spot < `{pos_summary['sl_put']:.0f}`"
        )
        header = "⚡ *[PAPER] SHORT STRANGLE*"
    else:
        leg    = pos_summary["legs"][0]
        emoji  = "🔴" if action == "CALL_SELL" else "🟢"
        legs_text = (
            f"   {emoji} `{leg['strike']} {leg['type']}` @ *₹{leg['entry']}*  ({leg['lots']} lots)"
        )
        sl_key  = "sl_call" if action == "CALL_SELL" else "sl_put"
        sl_text = f"   Spot {'>' if action=='CALL_SELL' else '<'} `{pos_summary[sl_key]:.0f}`"
        header  = f"{'🔴' if action=='CALL_SELL' else '🟢'} *[PAPER] {action.replace('_',' ')}*"

    reasons_text = "\n".join(f"   ✅ {r}" for r in signal_reasons) or "   (position management reversal)"

    text = "\n".join([
        f"📝 *PAPER ENTRY  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        header,
        legs_text,
        f"Spot: `{pos_summary['entry_spot']:.0f}`  |  Expiry: {pos_summary['expiry']}",
        "",
        "*Why this trade:*",
        reasons_text,
        "",
        "*⚠️ Stop Loss:*",
        sl_text,
        "*🎯 Target:* 65% premium decay",
        "━━━━━━━━━━━━━━━━━━━━",
        "_PAPER TRADE — no real money at risk_",
    ])
    return _post(text)


# ── Paper trade P&L update ────────────────────────────────────────────────────

def send_paper_pnl_update(pos_summary: dict, signal: Optional[FinalSignal] = None) -> bool:
    time_s, date_s = _now_str()
    action    = pos_summary["action"]
    total_pnl = pos_summary["total_pnl"]
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    pnl_str   = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"−₹{abs(total_pnl):,.0f}"

    legs_lines = []
    for l in pos_summary["legs"]:
        cur      = f"₹{l['current']:.1f}" if l["current"] is not None else "N/A"
        lpnl     = l["pnl"]
        lpnl_str = f"+₹{lpnl:,.0f}" if lpnl >= 0 else f"−₹{abs(lpnl):,.0f}"
        leg_pnl_emoji = "✅" if lpnl >= 0 else "⚠️"
        legs_lines.append(
            f"   {'🔴' if l['type']=='CE' else '🟢'} `{l['strike']} {l['type']}`:  "
            f"₹{l['entry']} → {cur}   {leg_pnl_emoji} {lpnl_str}"
        )

    realised_line = (
        [f"   + Realised (closed leg): +₹{pos_summary['realised_pnl']:,.0f}"]
        if pos_summary["realised_pnl"] != 0 else []
    )

    spot_move = pos_summary["current_spot"] - pos_summary["entry_spot"]
    spot_arrow = "▲" if spot_move >= 0 else "▼"

    lines = [
        f"📊 *[PAPER] P&L UPDATE  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Trade: *{action.replace('_',' ')}*  |  Expiry {pos_summary['expiry']}",
        f"Spot: `{pos_summary['current_spot']:.0f}` (entry `{pos_summary['entry_spot']:.0f}` {spot_arrow}{abs(spot_move):.0f} pts)",
        "",
        "*LEGS*",
        *legs_lines,
        *realised_line,
        "",
        f"{pnl_emoji} *Unrealised P&L: {pnl_str}*",
    ]

    if signal:
        lines += [
            "",
            f"*SIGNAL CHECK — vs your {action.replace('_',' ')}*",
            _signal_dashboard(signal, trade_action=action),
            "",
            _trade_verdict(action, signal),
        ]

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return _post("\n".join(lines))


# ── Paper trade exit ──────────────────────────────────────────────────────────

def send_paper_exit(pos_summary: dict, reason: str) -> bool:
    time_s, date_s = _now_str()
    total_pnl = pos_summary["realised_pnl"]
    pnl_emoji = "✅" if total_pnl >= 0 else "❌"
    pnl_str   = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"−₹{abs(total_pnl):,.0f}"

    REASON_LABEL = {
        "SL_CALL":        "🚨 Stop Loss hit — Call leg",
        "SL_PUT":         "🚨 Stop Loss hit — Put leg",
        "TARGET":         "🎯 Target reached — 65% decay",
        "FORCE_EXIT_255PM": "🔔 2:55 PM force exit",
    }
    reason_label = REASON_LABEL.get(reason, f"📌 {reason}")

    spot_move  = pos_summary["current_spot"] - pos_summary["entry_spot"]
    spot_arrow = "▲" if spot_move >= 0 else "▼"

    closed_legs = pos_summary.get("closed_legs", [])
    legs_lines = []
    for l in closed_legs:
        exit_s   = f"₹{l['exit']:.1f}" if l["exit"] is not None else "N/A"
        lpnl     = l["pnl"]
        lpnl_str = f"+₹{lpnl:,.0f}" if lpnl >= 0 else f"−₹{abs(lpnl):,.0f}"
        leg_emoji = "✅" if lpnl >= 0 else "❌"
        legs_lines.append(
            f"   {'🔴' if l['type']=='CE' else '🟢'} `{l['strike']} {l['type']}`:  "
            f"₹{l['entry']:.1f} → {exit_s}   {leg_emoji} {lpnl_str}"
        )

    text = "\n".join([
        f"🏁 *[PAPER] TRADE CLOSED  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Position: *{pos_summary['action'].replace('_',' ')}*",
        f"Exit reason: {reason_label}",
        f"Spot: entry `{pos_summary['entry_spot']:.0f}` → exit `{pos_summary['current_spot']:.0f}` ({spot_arrow}{abs(spot_move):.0f} pts)",
        "",
        *([" *LEGS*", *legs_lines, ""] if legs_lines else []),
        f"{pnl_emoji} *Realised P&L: {pnl_str}*",
        "━━━━━━━━━━━━━━━━━━━━",
        "_PAPER TRADE — no real money at risk_",
    ])
    return _post(text)


# ── Paper session summary ─────────────────────────────────────────────────────

def send_paper_session_summary(summary: dict) -> bool:
    time_s, date_s = _now_str()
    total   = summary["total_pnl"]
    pnl_str = f"+₹{total:,.0f}" if total >= 0 else f"−₹{abs(total):,.0f}"
    emoji   = "🎉" if total >= 0 else "📉"
    decided = summary["winners"] + summary["losers"]
    win_pct = round(summary["winners"] / decided * 100) if decided else 0
    flat    = summary.get("flat", 0)

    text = "\n".join([
        f"📋 *[PAPER] SESSION SUMMARY  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Trades   : {summary['total_trades']}",
        f"Winners  : {summary['winners']}  ({win_pct}% of decided trades)",
        f"Losers   : {summary['losers']}",
        *([ f"Flat (₹0): {flat}  _(LTP unavailable at exit)_" ] if flat else []),
        "",
        f"{emoji} *Session P&L: {pnl_str}*",
        "",
        f"Journal: `{Path(summary['journal_file']).name}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_Ready to go live? Run `python main.py`_",
    ])
    return _post(text)


# ── Trade warning ─────────────────────────────────────────────────────────────

def send_trade_warning(
    trade: TradeState,
    monitor: MonitorResult,
    spot: float,
    signal: Optional[FinalSignal] = None,
) -> bool:
    time_s, date_s = _now_str()
    has_critical = any(w.severity == "CRITICAL" for w in monitor.warnings)
    top_emoji    = "🚨" if has_critical else "⚠️"

    warning_lines = []
    for w in monitor.warnings:
        sev_emoji = "🚨" if w.severity == "CRITICAL" else "⚠️"
        warning_lines.append(f"   {sev_emoji} *{w.category}*: {w.detail}")

    lines = [
        f"{top_emoji} *TRADE WARNING  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Trade: *{trade.action.replace('_',' ')} {trade.strike}* @ ₹{trade.entry_premium}",
        f"Spot : `{spot:.0f}` (entry `{trade.entry_spot:.0f}`)  SL `{trade.sl_spot_level:.0f}`",
        "",
        f"*ALERTS ({len(monitor.warnings)})*",
        *warning_lines,
    ]

    if signal:
        lines += [
            "",
            f"*SIGNAL CHECK — vs your {trade.action.replace('_',' ')}*",
            _signal_dashboard(signal, trade_action=trade.action),
            "",
            _trade_verdict(trade.action, signal),
        ]
    else:
        lines += [
            "",
            f"PCR: `{monitor.current_pcr}`  ΔPCR: `{monitor.current_change_pcr}`",
            "_Review position. Adjust or exit if alerts persist._",
        ]

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return _post("\n".join(lines))


# ── Position management alert ─────────────────────────────────────────────────

def send_management_alert(decision, trade, spot: float, new_signal=None) -> bool:
    from signals.position_manager import (
        EXIT_CE_LEG, EXIT_PE_LEG, EXIT_FULL,
        REVERSE_CALL_SELL, REVERSE_PUT_SELL, SWITCH_STRANGLE,
    )
    time_s, date_s = _now_str()

    ACTION_EMOJI = {
        EXIT_CE_LEG: "⚡", EXIT_PE_LEG: "⚡", EXIT_FULL: "🚪",
        REVERSE_CALL_SELL: "🔄", REVERSE_PUT_SELL: "🔄", SWITCH_STRANGLE: "↔️",
    }
    ACTION_LABEL = {
        EXIT_CE_LEG: "EXIT CALL LEG — Hold Put",
        EXIT_PE_LEG: "EXIT PUT LEG — Hold Call",
        EXIT_FULL: "EXIT FULL POSITION",
        REVERSE_CALL_SELL: "REVERSE → CALL SELL",
        REVERSE_PUT_SELL: "REVERSE → PUT SELL",
        SWITCH_STRANGLE: "SWITCH → STRANGLE",
    }

    emoji = ACTION_EMOJI.get(decision.action, "⚠️")
    label = ACTION_LABEL.get(decision.action, decision.action)

    lines = [
        f"{emoji} *{label}  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Was: *{trade.action.replace('_',' ')} {trade.strike}* @ ₹{trade.entry_premium}",
        f"Spot entry `{trade.entry_spot:.0f}` → now `{spot:.0f}`",
        "",
        f"*Trigger ({decision.score}/3):*",
        *[f"   📌 {r}" for r in decision.reasons],
    ]

    if new_signal and new_signal.action not in ("NO_SIGNAL",):
        if new_signal.action == "STRANGLE":
            lines += [
                "", "*→ Entering STRANGLE:*",
                f"   🔴 SELL `{new_signal.call_strike} CE` @ ₹{new_signal.call_premium}",
                f"   🟢 SELL `{new_signal.put_strike} PE` @ ₹{new_signal.put_premium}",
                f"   {new_signal.strangle_lots} lots each leg",
            ]
        else:
            opt_t = "CE" if new_signal.action == "CALL_SELL" else "PE"
            lines += [
                "", f"*→ Entering {new_signal.action.replace('_',' ')}:*",
                f"   Strike `NIFTY {new_signal.strike} {opt_t}` @ ₹{new_signal.premium}",
                f"   Lots: {new_signal.lots}  SL: `{new_signal.sl_spot_level:.0f}`",
            ]

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return _post("\n".join(lines))


# ── S/R level approach alert ──────────────────────────────────────────────────

def send_level_approach_alert(trade_action: str, level, spot: float) -> bool:
    time_s, date_s = _now_str()
    pts_away   = abs(level.level - spot)
    level_type = level.sr_type.capitalize()

    if trade_action == "CALL_SELL":
        consequence = "if this resistance breaks, price could run higher — your Call Sell is at risk"
        action_hint = "Tighten SL or reduce position if price closes above it."
    elif trade_action == "PUT_SELL":
        consequence = "if this support breaks, price could fall further — your Put Sell is at risk"
        action_hint = "Tighten SL or reduce position if price closes below it."
    else:
        side        = "resistance" if level.sr_type == "resistance" else "support"
        consequence = f"price approaching {side} — one leg of your Strangle may come under pressure"
        action_hint = "Consider exiting the threatened leg if level breaks."

    text = "\n".join([
        f"⚠️ *LEVEL ALERT  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Your *{trade_action.replace('_', ' ')}* — Nifty approaching "
        f"{level_type} at *{level.level:.0f}*",
        "",
        f"Spot now `{spot:.1f}`  ({pts_away:.0f} pts away)",
        f"Level strength: *{level.strength}*  "
        f"({level.unique_dates} dates tested  |  {level.touches} touches)",
        "",
        f"_{consequence}._",
        f"_{action_hint}_",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    return _post(text)


# ── Strike roll / hedge alerts ───────────────────────────────────────────────

def send_paper_roll(pos_summary: dict, locked_pnl: float, old_strike: int,
                    new_strike: int, new_premium: float, spot: float) -> bool:
    direction = "UP ↑" if new_strike > old_strike else "DOWN ↓"
    text = "\n".join([
        f"🔄 *[PAPER] STRIKE ROLLED {direction}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Old: *{old_strike}*  →  New: *{new_strike}*",
        f"Locked P&L: ₹{locked_pnl:,.0f}  |  New premium: ₹{new_premium:.2f}",
        f"Spot: `{spot:.1f}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_Strike rolled to capture fresh premium at new support level._",
    ])
    return _post(text)


def send_paper_hedge_added(trade_action: str, hedge_strike: int,
                           hedge_premium: float, spot: float) -> bool:
    hedge_type = "CE" if trade_action == "PUT_SELL" else "PE"
    text = "\n".join([
        f"🛡️ *[PAPER] HEDGE LEG ADDED*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Original: *{trade_action}*  |  Added: *{hedge_type} {hedge_strike}* @ ₹{hedge_premium:.2f}",
        f"Spot: `{spot:.1f}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_2 consecutive reversal candles confirmed. Position now hedged._",
    ])
    return _post(text)


def send_paper_hedge_removed(trade_action: str, spot: float) -> bool:
    hedge_type = "CE" if trade_action == "PUT_SELL" else "PE"
    text = "\n".join([
        f"✅ *[PAPER] HEDGE LEG REMOVED*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Removed *{hedge_type}* hedge  |  Reverting to *{trade_action}*",
        f"Spot: `{spot:.1f}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "_2 consecutive clean candles — back to directional trade._",
    ])
    return _post(text)


# ── Live trade P&L update ─────────────────────────────────────────────────────

def send_live_pnl_update(
    trade,
    spot: float,
    oc,
    signal: Optional[FinalSignal] = None,
    strangle_legs=None,
) -> bool:
    time_s, date_s = _now_str()
    legs_lines: list[str] = []
    total_pnl = 0.0

    if trade.action == "STRANGLE" and strangle_legs:
        for strike, data_dict, active_attr, entry_attr, emoji, opt_type in [
            (strangle_legs.ce_strike, oc.call_data, "ce_active", "ce_entry_premium", "🔴", "CE"),
            (strangle_legs.pe_strike, oc.put_data,  "pe_active", "pe_entry_premium", "🟢", "PE"),
        ]:
            if not getattr(strangle_legs, active_attr):
                continue
            sd    = data_dict.get(strike)
            ltp   = sd.ltp if sd and sd.ltp > 0 else None
            entry = getattr(strangle_legs, entry_attr)
            pnl   = (entry - (ltp or entry)) * trade.lots
            total_pnl += pnl
            cur_s = f"₹{ltp:.1f}" if ltp else "N/A"
            pnl_s = f"+₹{pnl:,.0f}" if pnl >= 0 else f"−₹{abs(pnl):,.0f}"
            legs_lines.append(
                f"   {emoji} `{strike} {opt_type}`:  "
                f"₹{entry} → {cur_s}   {'✅' if pnl >= 0 else '⚠️'} {pnl_s}"
            )
    else:
        data_dict = oc.call_data if trade.action == "CALL_SELL" else oc.put_data
        opt_type  = "CE" if trade.action == "CALL_SELL" else "PE"
        emoji     = "🔴" if trade.action == "CALL_SELL" else "🟢"
        sd        = data_dict.get(trade.strike)
        ltp       = sd.ltp if sd and sd.ltp > 0 else None
        total_pnl = (trade.entry_premium - (ltp or trade.entry_premium)) * trade.lots
        cur_s     = f"₹{ltp:.1f}" if ltp else "N/A"
        pnl_s     = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"−₹{abs(total_pnl):,.0f}"
        legs_lines.append(
            f"   {emoji} `{trade.strike} {opt_type}`:  "
            f"₹{trade.entry_premium} → {cur_s}   {'✅' if total_pnl >= 0 else '⚠️'} {pnl_s}"
        )

    pnl_emoji  = "📈" if total_pnl >= 0 else "📉"
    pnl_str    = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"−₹{abs(total_pnl):,.0f}"
    spot_move  = spot - trade.entry_spot
    spot_arrow = "▲" if spot_move >= 0 else "▼"

    lines = [
        f"📊 *[LIVE] P&L UPDATE  |  {time_s}  |  {date_s}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Trade: *{trade.action.replace('_', ' ')}*  |  Expiry {trade.expiry}",
        f"Spot: `{spot:.0f}` (entry `{trade.entry_spot:.0f}` {spot_arrow}{abs(spot_move):.0f} pts)",
        "",
        "*LEGS*",
        *legs_lines,
        "",
        f"{pnl_emoji} *Unrealised P&L: {pnl_str}*",
    ]

    if signal:
        lines += [
            "",
            f"*SIGNAL CHECK — vs your {trade.action.replace('_', ' ')}*",
            _signal_dashboard(signal, trade_action=trade.action),
            "",
            _trade_verdict(trade.action, signal),
        ]

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return _post("\n".join(lines))


# ── Trade journal record ─────────────────────────────────────────────────────

def send_trade_journal_entry(
    trade,
    exit_ltp:       Optional[float],
    hedge_exit_ltp: Optional[float],
    main_pnl:       float,
    hedge_pnl:      float,
    total_pnl:      float,
    spot:           float,
    exit_reason:    str,
) -> bool:
    time_s, date_s = _now_str()
    pnl_emoji = "✅" if total_pnl >= 0 else "❌"
    opt_type  = "CE" if trade.action in ("CALL_SELL", "STRANGLE_CE") else "PE"

    def _fmt(p: float) -> str:
        return f"+₹{p:,.0f}" if p >= 0 else f"−₹{abs(p):,.0f}"

    REASON_LABEL = {
        "FORCE_EXIT_255PM": "2:55 PM force exit",
        "FORCE_EXIT_325PM": "3:25 PM force exit",
        "SL_HIT":         "Stop loss hit",
        "MANAGEMENT_EXIT":"Position management exit",
        "STRANGLE_TARGET":     "Target hit — 65% premium decay",
        "STRANGLE_SIGNAL_EXIT":"Signal-based full exit",
    }

    entry_spot_s = f"{trade.entry_spot:.0f}" if trade.entry_spot > 0 else "—"
    entry_time_s = trade.entry_time.strftime("%H:%M IST") if trade.entry_time else "—"
    exit_price_s = f"₹{exit_ltp:.2f}" if exit_ltp else "~market"
    lots_count   = trade.lots // 65

    lines = [
        f"📋 *TRADE RECORD  |  {date_s}*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"*{trade.action.replace('_', ' ')} {trade.strike} {opt_type}*  |  Expiry {trade.expiry}",
        f"",
        f"`{'Entry':<8}` {entry_time_s}  spot `{entry_spot_s}`  @ ₹{trade.entry_premium:.2f}",
        f"`{'Exit':<8}` {time_s}  spot `{spot:.0f}`  @ {exit_price_s}",
        f"`{'Qty':<8}` {trade.lots} shares  ({lots_count} lot{'s' if lots_count != 1 else ''})",
        f"",
        f"*P&L BREAKDOWN*",
        f"   Sold `{trade.strike}{opt_type}`:   {_fmt(main_pnl)}",
    ]
    if trade.hedge_entry_premium is not None:
        hedge_exit_s = f"₹{hedge_exit_ltp:.2f}" if hedge_exit_ltp else "~market"
        lines.append(f"   Hedge `{trade.hedge_strike}{opt_type}`:  {_fmt(hedge_pnl)}")
    lines += [
        f"   {'─' * 20}",
        f"   {pnl_emoji} *Net P&L:  {_fmt(total_pnl)}*",
        f"",
        f"Exit: _{REASON_LABEL.get(exit_reason, exit_reason)}_",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"_Logged to trade\\_journal.csv_",
    ]
    return _post("\n".join(lines))


# ── Error alert ───────────────────────────────────────────────────────────────

def send_error_alert(message: str) -> None:
    _post(f"⚠️ *Bot Error*\n`{message}`")
