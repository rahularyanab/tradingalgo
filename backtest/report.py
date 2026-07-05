"""
Generates backtest report: trade log + performance statistics.
Saves to logs/backtest_report_<date>.csv and prints summary to console.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from backtest.engine import BacktestTrade

logger = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).parent.parent / "logs"


def generate_detailed_report(trades: list, start_date: str, end_date: str):
    """Print a full per-trade event log — entry, management actions, exit."""
    separator = "═" * 62
    thin      = "─" * 62

    ACTION_LABEL = {
        "EXIT_CE_LEG":       "⚡ EXIT CALL LEG  (hold put)",
        "EXIT_PE_LEG":       "⚡ EXIT PUT LEG   (hold call)",
        "EXIT_FULL":         "🚪 EXIT FULL",
        "REVERSE_CALL_SELL": "🔄 REVERSE → CALL SELL",
        "REVERSE_PUT_SELL":  "🔄 REVERSE → PUT SELL",
        "SWITCH_STRANGLE":   "↔️  SWITCH → STRANGLE",
        "ENTRY":             "📥 ENTRY",
        "EXIT":              "📤 EXIT",
        "HOLD":              "   HOLD",
    }

    print(f"\n{separator}")
    print(f"  DETAILED TRADE LOG  |  {start_date} → {end_date}")
    print(separator)

    for t in trades:
        pnl    = t.pnl_total or 0
        emoji  = "✅" if pnl >= 0 else "❌"
        pnl_s  = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"

        if t.action == "STRANGLE":
            strike_s = f"CE {t.strike} + PE {t.put_strike}"
        else:
            strike_s = f"{t.action.replace('_',' ')}  {t.strike}  ({t.lots}L)"

        print(f"\n  TRADE #{t.trade_id}  |  {strike_s}")
        print(f"  Entry : {t.entry_time.strftime('%d %b %H:%M')}  spot={t.entry_spot:.0f}  prem=₹{t.entry_premium}")
        if t.action == "STRANGLE":
            print(f"  SL    : CE>{t.sl_spot:.0f}  PE<{t.put_sl:.0f}  |  Target: ₹{t.target_premium:.1f}")
        else:
            sl_dir = ">" if t.action == "CALL_SELL" else "<"
            print(f"  SL    : spot {sl_dir} {t.sl_spot:.0f}  |  Target: ₹{t.target_premium:.1f}")
        print(f"  {thin}")

        managed = [e for e in (t.events or []) if e.action not in ("ENTRY", "EXIT", "HOLD")]
        if managed:
            print(f"  Management actions:")
            for ev in managed:
                label = ACTION_LABEL.get(ev.action, ev.action)
                print(f"    {ev.time.strftime('%d %b %H:%M')}  {label}")
                short = ev.reason[:70] + "…" if len(ev.reason) > 70 else ev.reason
                print(f"                  → {short}")
                if ev.action == "EXIT_CE_LEG":
                    print(f"                  → CE leg P&L: ₹{t.ce_closed_pnl:,.0f}")
                elif ev.action == "EXIT_PE_LEG":
                    print(f"                  → PE leg P&L: ₹{t.pe_closed_pnl:,.0f}")
        else:
            print(f"  No management actions — held to natural exit")

        print(f"  {thin}")
        exit_t = t.exit_time.strftime('%d %b %H:%M') if t.exit_time else "-"
        exit_spot_s = f"{t.exit_spot:.0f}" if t.exit_spot else "—"
        print(f"  Exit  : {exit_t}  reason={t.exit_reason}  spot={exit_spot_s}")
        print(f"  {emoji} P&L: {pnl_s}  (lot pnl=₹{t.pnl_per_lot or 0:,.2f} × {t.lots}L × 75)")
        print(f"  {separator}")

    # Quick summary
    wins   = [t for t in trades if (t.pnl_total or 0) > 0]
    losses = [t for t in trades if (t.pnl_total or 0) <= 0]
    total  = sum(t.pnl_total or 0 for t in trades)
    managed_count = sum(1 for t in trades if any(e.action not in ("ENTRY","EXIT","HOLD") for e in (t.events or [])))
    print(f"\n  SUMMARY: {len(trades)} trades  |  {len(wins)}W / {len(losses)}L  |  Total P&L: ₹{total:,.0f}")
    print(f"  Trades with management action: {managed_count}")
    print(f"{separator}\n")


def generate_report(
    trades:     list,
    start_date: str,
    end_date:   str,
    sigma:      float,
    lot_size:   int,
    detail:     bool = False,
) -> dict:
    if not trades:
        print("\nNo trades executed in the backtest period.")
        return {}

    df = pd.DataFrame([
        {
            "id":            t.trade_id,
            "entry_time":    t.entry_time,
            "action":        t.action,
            "strike":        t.strike,
            "lots":          t.lots,
            "expiry":        t.expiry,
            "entry_spot":    t.entry_spot,
            "entry_premium": t.entry_premium,
            "sl_spot":       t.sl_spot,
            "target_premium":t.target_premium,
            "exit_time":     t.exit_time,
            "exit_spot":     t.exit_spot,
            "exit_premium":  t.exit_premium,
            "exit_reason":   t.exit_reason,
            "pnl_per_lot":   t.pnl_per_lot,
            "pnl_total":     t.pnl_total,
            "hold_hours":    (
                (t.exit_time - t.entry_time).total_seconds() / 3600
                if t.exit_time and t.entry_time else None
            ),
        }
        for t in trades
    ])

    # ── Statistics ────────────────────────────────────────────────
    wins   = df[df["pnl_total"] > 0]
    losses = df[df["pnl_total"] <= 0]

    total_pnl      = df["pnl_total"].sum()
    win_rate       = len(wins) / len(df) * 100
    avg_win        = wins["pnl_total"].mean()   if len(wins)   else 0
    avg_loss       = losses["pnl_total"].mean() if len(losses) else 0
    gross_profit   = wins["pnl_total"].sum()    if len(wins)   else 0
    gross_loss     = abs(losses["pnl_total"].sum()) if len(losses) else 0
    profit_factor  = gross_profit / gross_loss if gross_loss else float("inf")

    # Cumulative P&L for drawdown calculation
    cumulative     = df["pnl_total"].cumsum()
    rolling_max    = cumulative.cummax()
    drawdowns      = cumulative - rolling_max
    max_drawdown   = drawdowns.min()

    # Exit reason breakdown
    reason_counts = df["exit_reason"].value_counts().to_dict()
    action_counts  = df["action"].value_counts().to_dict()
    strangles      = df[df["action"] == "STRANGLE"]

    stats = {
        "period":           f"{start_date} → {end_date}",
        "sigma_iv":         f"{sigma*100:.1f}%",
        "lot_size":         lot_size,
        "total_trades":     len(df),
        "call_sells":       action_counts.get("CALL_SELL", 0),
        "put_sells":        action_counts.get("PUT_SELL", 0),
        "strangles":        action_counts.get("STRANGLE", 0),
        "winners":          len(wins),
        "losers":           len(losses),
        "win_rate":         f"{win_rate:.1f}%",
        "total_pnl":        f"₹{total_pnl:,.2f}",
        "avg_win":          f"₹{avg_win:,.2f}",
        "avg_loss":         f"₹{avg_loss:,.2f}",
        "profit_factor":    f"{profit_factor:.2f}",
        "max_drawdown":     f"₹{max_drawdown:,.2f}",
        "targets_hit":      reason_counts.get("TARGET", 0),
        "sl_hit":           reason_counts.get("SL", 0),
        "expired":          reason_counts.get("EXPIRY", 0),
        "avg_hold_hours":   f"{df['hold_hours'].mean():.1f}h",
    }

    # ── Console output ────────────────────────────────────────────
    separator = "═" * 52
    print(f"\n{separator}")
    print(f"  BACKTEST REPORT  |  {stats['period']}")
    print(f"  IV: {stats['sigma_iv']}  |  Lot size: {lot_size}")
    print(separator)
    print(f"  Total trades     : {stats['total_trades']}  "
          f"({stats['call_sells']} CALL SELL / {stats['put_sells']} PUT SELL / {stats['strangles']} STRANGLE)")
    print(f"  Win rate         : {stats['win_rate']}  "
          f"({stats['winners']} wins / {stats['losers']} losses)")
    print(f"  Total P&L        : {stats['total_pnl']}")
    print(f"  Avg win          : {stats['avg_win']}")
    print(f"  Avg loss         : {stats['avg_loss']}")
    print(f"  Profit factor    : {stats['profit_factor']}")
    print(f"  Max drawdown     : {stats['max_drawdown']}")
    print(separator)
    print(f"  Targets hit      : {stats['targets_hit']}")
    print(f"  SL hit           : {stats['sl_hit']}")
    print(f"  Expired          : {stats['expired']}")
    print(f"  Avg hold time    : {stats['avg_hold_hours']}")
    print(separator)

    # ── Save trade log ────────────────────────────────────────────
    REPORT_DIR.mkdir(exist_ok=True)
    filename = REPORT_DIR / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(filename, index=False)
    print(f"\n  Trade log saved: {filename}\n")

    if detail:
        generate_detailed_report(trades, start_date, end_date)

    return stats
