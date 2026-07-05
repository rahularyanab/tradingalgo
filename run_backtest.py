"""
Nifty Option Selling — Backtest Runner
=======================================
Usage:
  python run_backtest.py --start 2024-01-01 --end 2026-05-30
  python run_backtest.py --start 2024-01-01 --end 2026-05-30 --iv 16
  python run_backtest.py --start 2024-01-01 --end 2026-05-30 --source zerodha
  python run_backtest.py --start 2024-01-01 --end 2026-05-30 --no-cache

Arguments:
  --start     Start date (YYYY-MM-DD)  [required]
  --end       End date   (YYYY-MM-DD)  [required]
  --iv        Implied volatility %     [default: 14]
  --source    Data source: yfinance (default, free) | zerodha (requires login)
  --no-cache  Force re-fetch, ignore cached CSV

Note: Signals use Trendline S&R + RSI Divergence only.
Historical option chain OI is unavailable, so the OI/PCR signal is
excluded from backtest entries (entry requires 2/2 instead of live 3/3).
"""

import argparse
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backtest")


def parse_args():
    p = argparse.ArgumentParser(description="Nifty option selling backtest")
    p.add_argument("--start",    required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end",      required=True, help="End date   YYYY-MM-DD")
    p.add_argument("--iv",       type=float, default=14.0, help="IV %% (default 14)")
    p.add_argument("--source",   default="yfinance", choices=["yfinance", "zerodha"],
                   help="Data source (default: yfinance — no credentials needed)")
    p.add_argument("--no-cache", action="store_true", help="Ignore cached CSV")
    p.add_argument("--detail",   action="store_true", help="Show per-trade event log with management actions")
    p.add_argument("--proximity", type=float, default=None,
                   help="Trendline proximity %% override (default: 0.3 for 15m, 1.5 for daily)")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        end_dt   = datetime.strptime(args.end,   "%Y-%m-%d")
    except ValueError:
        logger.error("Dates must be YYYY-MM-DD format.")
        sys.exit(1)

    if start_dt >= end_dt:
        logger.error("--start must be before --end.")
        sys.exit(1)

    sigma = args.iv / 100.0
    logger.info(f"Backtest: {start_dt.date()} → {end_dt.date()}  IV={args.iv}%  source={args.source}")

    # ── Zerodha client (only when source=zerodha) ─────────────────
    kite = None
    if args.source == "zerodha":
        from auth.kite_login import load_access_token
        from data.market_data import get_kite_client
        try:
            token = load_access_token()
            kite  = get_kite_client(token)
        except (FileNotFoundError, ValueError) as e:
            logger.error(str(e))
            sys.exit(1)

    # ── Fetch historical data ─────────────────────────────────────
    from backtest.data_loader import fetch_historical_data
    df = fetch_historical_data(
        start_date=start_dt,
        end_date=end_dt,
        source=args.source,
        use_cache=not args.no_cache,
        kite=kite,
    )

    # Market hours filter — only applies to intraday data (index has time component)
    if hasattr(df.index, "hour") and df.index.hour.max() > 0:
        df = df.between_time("09:15", "15:30")
        logger.info(f"Market-hours candles: {len(df)}")
    else:
        logger.info(f"Daily data — skipping time filter. Candles: {len(df)}")

    if len(df) < 100:
        logger.error("Not enough candles to run backtest (need at least 100).")
        sys.exit(1)

    # ── Auto-tune proximity for daily data ───────────────────────
    import config as cfg
    is_daily = not (hasattr(df.index, "hour") and df.index.hour.max() > 0)
    if args.proximity is not None:
        cfg.PROXIMITY_PCT = args.proximity / 100.0
        logger.info(f"Proximity override: {args.proximity}%")
    elif is_daily:
        cfg.PROXIMITY_PCT = 0.015   # 1.5% band suits daily trendlines
        cfg.PIVOT_LOOKBACK = 3      # 3 days each side for daily pivots
        logger.info("Daily mode: proximity=1.5%  pivot_lookback=3")

    # ── Run backtest ──────────────────────────────────────────────
    from backtest.engine import BacktestEngine
    engine = BacktestEngine(df=df, sigma=sigma, use_position_manager=True)
    trades = engine.run()

    # ── Generate report ───────────────────────────────────────────
    from backtest.report import generate_report
    from config import NIFTY_LOT_SIZE
    generate_report(
        trades=trades,
        start_date=args.start,
        end_date=args.end,
        sigma=sigma,
        lot_size=NIFTY_LOT_SIZE,
        detail=args.detail,
    )


if __name__ == "__main__":
    main()
