"""
Aggregates the bot's own trade journal (logs/trade_journal.csv, written by
main.py's _record_trade_exit) into daily P&L totals.

Only ever reflects trades this bot itself entered and exited — never manual
positions or anything else sitting in the Kite account.
"""

import csv
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

JOURNAL_PATH = Path(__file__).parent.parent / "logs" / "trade_journal.csv"


class DayPnL(NamedTuple):
    date: str
    net_pnl: float
    trades: int


def _read_rows() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    with open(JOURNAL_PATH, newline="") as f:
        return list(csv.DictReader(f))


def daily_pnl(year: int, month: int) -> list[DayPnL]:
    """Daily net P&L for the bot's own trades in the given month, oldest first."""
    by_day: "OrderedDict[str, list[float]]" = OrderedDict()
    for r in _read_rows():
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d.year != year or d.month != month:
            continue
        try:
            pnl = float(r["net_pnl"] or 0)
        except ValueError:
            continue
        by_day.setdefault(r["date"], []).append(pnl)

    return [
        DayPnL(date=day, net_pnl=sum(vals), trades=len(vals))
        for day, vals in sorted(by_day.items())
    ]


def monthly_summary(year: int, month: int) -> tuple[list[DayPnL], float, int]:
    """(daily breakdown, total net P&L, total trade count) for the given month."""
    days = daily_pnl(year, month)
    total = sum(d.net_pnl for d in days)
    trades = sum(d.trades for d in days)
    return days, total, trades
