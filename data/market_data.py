import logging
from datetime import datetime, timedelta

import pandas as pd
from kiteconnect import KiteConnect

from config import (
    KITE_API_KEY,
    NIFTY_INSTRUMENT_TOKEN,
    TIMEFRAME,
    CANDLE_COUNT,
)

logger = logging.getLogger(__name__)


def get_kite_client(access_token: str) -> KiteConnect:
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(access_token)
    return kite


def fetch_nifty_candles(kite: KiteConnect) -> pd.DataFrame:
    """Fetch last N 15-min OHLCV candles for Nifty 50."""
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=10)   # enough history for pivots + RSI warmup

    try:
        records = kite.historical_data(
            instrument_token=NIFTY_INSTRUMENT_TOKEN,
            from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            interval=TIMEFRAME,
            continuous=False,
        )
    except Exception as e:
        logger.error(f"Failed to fetch candles: {e}")
        raise

    df = pd.DataFrame(records)
    df.rename(columns={"date": "datetime"}, inplace=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].tail(CANDLE_COUNT)
    logger.info(f"Fetched {len(df)} candles. Latest: {df.index[-1]}")
    return df


def get_current_nifty_price(kite: KiteConnect) -> float:
    """Return latest Nifty 50 price. Uses quote() which works after market close too."""
    data = kite.quote("NSE:NIFTY 50")
    return data["NSE:NIFTY 50"]["last_price"]
