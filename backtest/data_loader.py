"""
Fetches Nifty 15-min OHLCV history.

Sources:
  "yfinance"  — Yahoo Finance (^NSEI). Free, no auth needed. Default for backtesting.
  "zerodha"   — KiteConnect API. Requires valid access token.

Both sources cache results to CSV to avoid redundant fetches.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR  = Path(__file__).parent.parent / "logs" / "cache"
CHUNK_DAYS = 180   # Zerodha 15-min limit is ~200 days per request


def _cache_path(source: str, start: datetime, end: datetime) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"nifty15_{source}_{start.date()}_{end.date()}.csv"


# ── Yahoo Finance source ───────────────────────────────────────────────────────

def _yf_interval_and_chunk(start_date: datetime, end_date: datetime) -> tuple[str, int]:
    """
    Yahoo Finance intraday limits:
      15m  → last 60 days only
      60m  → last 730 days
      1d   → unlimited
    Pick the finest interval that covers the full requested range.
    """
    days = (end_date - start_date).days
    now  = datetime.now()
    oldest_start = (now - start_date).days

    if oldest_start <= 60:
        return "15m", 55        # 15-min, chunk by 55 days
    elif oldest_start <= 725:
        return "60m", 350       # 1-hour, chunk by 350 days
    else:
        return "1d", 1800       # daily, chunk by 1800 days


def _fetch_yfinance(start_date: datetime, end_date: datetime) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("Run: pip install yfinance")

    interval, chunk_days = _yf_interval_and_chunk(start_date, end_date)
    logger.info(
        f"Yahoo Finance ^NSEI [{interval}]: {start_date.date()} → {end_date.date()} "
        f"(chunks of {chunk_days} days)"
    )
    if interval != "15m":
        logger.info(
            f"Note: Yahoo Finance 15-min data is limited to last 60 days. "
            f"Using {interval} interval for this date range — signals remain valid."
        )

    all_chunks = []
    chunk_start = start_date

    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end_date)
        try:
            ticker = yf.Ticker("^NSEI")
            chunk  = ticker.history(
                start=chunk_start.strftime("%Y-%m-%d"),
                end=chunk_end.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=True,
            )
            if not chunk.empty:
                if hasattr(chunk.index, "tz") and chunk.index.tz is not None:
                    chunk.index = chunk.index.tz_localize(None)
                chunk = chunk.rename(columns={
                    "Open": "open", "High": "high",
                    "Low":  "low",  "Close": "close",
                    "Volume": "volume",
                })[["open", "high", "low", "close", "volume"]]
                all_chunks.append(chunk)
                logger.info(f"  {chunk_start.date()} → {chunk_end.date()}: {len(chunk)} bars")
        except Exception as e:
            logger.warning(f"  chunk failed ({chunk_start.date()}): {e}")
        chunk_start = chunk_end + timedelta(days=1)

    if not all_chunks:
        raise ValueError("No data returned from Yahoo Finance. Check date range.")

    df = pd.concat(all_chunks).sort_index().drop_duplicates()
    logger.info(f"Yahoo Finance: {len(df)} {interval} candles fetched.")
    return df


# ── Zerodha source ─────────────────────────────────────────────────────────────

def _fetch_zerodha(kite, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    from config import NIFTY_INSTRUMENT_TOKEN, TIMEFRAME

    all_chunks = []
    chunk_start = start_date

    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end_date)
        logger.info(f"  chunk {chunk_start.date()} → {chunk_end.date()}")
        try:
            records = kite.historical_data(
                instrument_token=NIFTY_INSTRUMENT_TOKEN,
                from_date=chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                interval=TIMEFRAME,
                continuous=False,
            )
            if records:
                chunk_df = pd.DataFrame(records)
                chunk_df.rename(columns={"date": "datetime"}, inplace=True)
                chunk_df["datetime"] = pd.to_datetime(chunk_df["datetime"])
                chunk_df.set_index("datetime", inplace=True)
                all_chunks.append(chunk_df[["open", "high", "low", "close", "volume"]])
        except Exception as e:
            logger.error(f"  chunk failed ({chunk_start.date()}): {e}")
        chunk_start = chunk_end + timedelta(days=1)

    if not all_chunks:
        raise ValueError("No data from Zerodha. Check credentials and date range.")

    df = pd.concat(all_chunks).sort_index().drop_duplicates()
    logger.info(f"Zerodha: {len(df)} 15-min candles fetched.")
    return df


# ── Public interface ───────────────────────────────────────────────────────────

def fetch_historical_data(
    start_date: datetime,
    end_date:   datetime,
    source:     str  = "yfinance",
    use_cache:  bool = True,
    kite              = None,
) -> pd.DataFrame:
    """
    Fetch Nifty 15-min OHLCV for [start_date, end_date].

    Args:
        source:    "yfinance" (default, free) or "zerodha" (requires kite client)
        use_cache: Load from CSV cache if available
        kite:      KiteConnect client (only needed when source="zerodha")
    """
    cache_file = _cache_path(source, start_date, end_date)

    if use_cache and cache_file.exists():
        logger.info(f"Loading cached data: {cache_file.name}")
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        logger.info(f"Cache: {len(df)} candles loaded.")
        return df

    if source == "yfinance":
        df = _fetch_yfinance(start_date, end_date)
    elif source == "zerodha":
        if kite is None:
            raise ValueError("kite client required for source='zerodha'")
        df = _fetch_zerodha(kite, start_date, end_date)
    else:
        raise ValueError(f"Unknown source '{source}'. Use 'yfinance' or 'zerodha'.")

    df.to_csv(cache_file)
    logger.info(f"Cached to {cache_file.name}")
    return df
