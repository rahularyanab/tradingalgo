"""
Fetches Nifty option chain — NSE public API primary, Kite Connect fallback.
Parses OI, Change-in-OI, LTP, PCR, max pain, and call/put walls.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}

NSE_BASE   = "https://www.nseindia.com"
NSE_OC_URL = f"{NSE_BASE}/api/option-chain-indices?symbol=NIFTY"

MIN_LTP_THRESHOLD    = 10.0   # ignore options with LTP < this
TRADEABLE_LTP_MIN    = 30.0   # preferred minimum LTP for selling


@dataclass
class StrikeData:
    strike:         int
    oi:             float
    change_oi:      float    # positive = fresh positions added
    pct_change_oi:  float
    ltp:            float
    iv:             float
    volume:         int


@dataclass
class OptionChainData:
    spot_price:         float
    pcr:                float          # total put OI / total call OI
    change_pcr:         float          # (sum put COI) / (sum call COI) — momentum PCR
    max_pain:           int
    call_wall:          int            # strike with max call OI
    put_wall:           int            # strike with max put OI
    atm_strike:         int
    weekly_expiry_date: str
    call_data:          dict = field(default_factory=dict)   # strike → StrikeData
    put_data:           dict = field(default_factory=dict)   # strike → StrikeData
    top_2_calls:        list = field(default_factory=list)   # top-2 by OI, LTP > TRADEABLE_LTP_MIN
    top_2_puts:         list = field(default_factory=list)   # top-2 by OI, LTP > TRADEABLE_LTP_MIN


def _create_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    for url in [NSE_BASE, f"{NSE_BASE}/option-chain"]:
        try:
            session.get(url, timeout=12)
            time.sleep(0.8)
        except Exception as e:
            logger.warning(f"NSE session warmup ({url}): {e}")
    return session


def _fetch_with_retry(retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        session = _create_nse_session()
        try:
            resp = session.get(NSE_OC_URL, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Option chain attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise Exception(f"Option chain fetch failed after {retries} attempts")


def _get_weekly_expiry(expiries: list[str]) -> str:
    """
    Returns the nearest upcoming expiry.
    On expiry day (Tuesday): skips today and returns NEXT week's expiry
    so we never trade same-day expiry options.
    """
    from datetime import datetime
    today     = datetime.now().date()
    is_expiry = (today.weekday() == 1)   # 1 = Tuesday

    parsed = []
    for expiry_str in expiries:
        for fmt in ["%d-%b-%Y", "%Y-%m-%d"]:
            try:
                d = datetime.strptime(expiry_str, fmt).date()
                parsed.append((d, expiry_str))
                break
            except ValueError:
                continue

    parsed.sort()
    for d, expiry_str in parsed:
        if is_expiry and d == today:
            continue          # skip today's expiry on expiry day
        if d >= today:
            return expiry_str

    return expiries[0]


def _calculate_max_pain(call_oi: dict, put_oi: dict) -> int:
    all_strikes = sorted(set(call_oi) | set(put_oi))
    if not all_strikes:
        return 0
    min_loss = float("inf")
    max_pain_s = all_strikes[0]
    for test in all_strikes:
        loss = sum((test - s) * oi for s, oi in call_oi.items() if test > s)
        loss += sum((s - test) * oi for s, oi in put_oi.items() if test < s)
        if loss < min_loss:
            min_loss = loss
            max_pain_s = test
    return max_pain_s


def fetch_option_chain(kite=None) -> Optional[OptionChainData]:
    try:
        data = _fetch_with_retry()
    except Exception as e:
        logger.error(f"Option chain fetch failed: {e}")
        if kite is not None:
            logger.info("NSE blocked — falling back to Kite option chain")
            return _fetch_option_chain_from_kite(kite)
        return None

    try:
        records    = data["records"]
        spot_price = float(records["underlyingValue"])
        expiries   = records["expiryDates"]
        weekly_exp = _get_weekly_expiry(expiries)

        filtered = [
            r for r in records["data"]
            if r.get("expiryDate") == weekly_exp
        ]
        if not filtered:
            logger.error("No option chain data for weekly expiry")
            return None

        call_data: dict[int, StrikeData] = {}
        put_data:  dict[int, StrikeData] = {}

        for row in filtered:
            strike = int(row["strikePrice"])

            if "CE" in row:
                ce = row["CE"]
                call_data[strike] = StrikeData(
                    strike        = strike,
                    oi            = ce.get("openInterest", 0),
                    change_oi     = ce.get("changeinOpenInterest", 0),
                    pct_change_oi = ce.get("pchangeinOpenInterest", 0),
                    ltp           = ce.get("lastPrice", 0),
                    iv            = ce.get("impliedVolatility", 0),
                    volume        = ce.get("totalTradedVolume", 0),
                )

            if "PE" in row:
                pe = row["PE"]
                put_data[strike] = StrikeData(
                    strike        = strike,
                    oi            = pe.get("openInterest", 0),
                    change_oi     = pe.get("changeinOpenInterest", 0),
                    pct_change_oi = pe.get("pchangeinOpenInterest", 0),
                    ltp           = pe.get("lastPrice", 0),
                    iv            = pe.get("impliedVolatility", 0),
                    volume        = pe.get("totalTradedVolume", 0),
                )

        # ── PCR ──────────────────────────────────────────────────
        total_call_oi = sum(s.oi        for s in call_data.values())
        total_put_oi  = sum(s.oi        for s in put_data.values())
        total_call_coi = sum(max(s.change_oi, 0) for s in call_data.values())
        total_put_coi  = sum(max(s.change_oi, 0) for s in put_data.values())

        pcr        = round(total_put_oi / total_call_oi, 2) if total_call_oi else 0
        change_pcr = round(total_put_coi / total_call_coi, 2) if total_call_coi else 0

        # ── Walls ─────────────────────────────────────────────────
        call_wall = max(call_data, key=lambda s: call_data[s].oi, default=0)
        put_wall  = max(put_data,  key=lambda s: put_data[s].oi,  default=0)

        # ── Max pain ──────────────────────────────────────────────
        max_pain = _calculate_max_pain(
            {s: d.oi for s, d in call_data.items()},
            {s: d.oi for s, d in put_data.items()},
        )

        # ── ATM ───────────────────────────────────────────────────
        atm_strike = round(spot_price / 50) * 50

        # ── Top-2 by OI (price > TRADEABLE_LTP_MIN, exclude < MIN_LTP_THRESHOLD) ──
        top_2_calls = sorted(
            [s for s in call_data.values() if s.ltp >= TRADEABLE_LTP_MIN],
            key=lambda s: s.oi, reverse=True
        )[:2]

        top_2_puts = sorted(
            [s for s in put_data.values() if s.ltp >= TRADEABLE_LTP_MIN],
            key=lambda s: s.oi, reverse=True
        )[:2]

        logger.info(
            f"OC: spot={spot_price} pcr={pcr} change_pcr={change_pcr} "
            f"call_wall={call_wall} put_wall={put_wall} max_pain={max_pain} "
            f"top_calls={[s.strike for s in top_2_calls]} "
            f"top_puts={[s.strike for s in top_2_puts]}"
        )

        return OptionChainData(
            spot_price=spot_price,
            pcr=pcr,
            change_pcr=change_pcr,
            max_pain=max_pain,
            call_wall=call_wall,
            put_wall=put_wall,
            atm_strike=atm_strike,
            weekly_expiry_date=weekly_exp,
            call_data=call_data,
            put_data=put_data,
            top_2_calls=top_2_calls,
            top_2_puts=top_2_puts,
        )

    except Exception as e:
        logger.exception(f"Option chain parse error: {e}")
        return None


# ── Kite-based option chain (fallback when NSE API is blocked) ────────────────
# Symbol format varies: weekly = NIFTY{YY}{M}{DD:02d}{STRIKE}{TYPE}
#                       monthly = NIFTY{YY}{MMM}{STRIKE}{TYPE}
# Safest: look up tradingsymbol directly from instruments list.
#
# OI note: Kite quote() has no oi_day_change field. We compute change_oi as the
# delta from the previous scan's OI snapshot (15-min delta). This captures
# intraday momentum better than a zero placeholder.

_kite_instruments_cache: dict = {}
_oi_snapshot: dict = {}   # {symbol: oi} from last scan, used to compute change_oi


def _get_nifty_instruments(kite) -> list:
    today = datetime.now().date()
    cache = _kite_instruments_cache
    if cache.get("date") == today:
        return cache["instruments"]
    instruments = kite.instruments("NFO")
    nifty = [
        i for i in instruments
        if i.get("name") == "NIFTY"
        and i.get("instrument_type") in ("CE", "PE")
        and i.get("expiry") and i["expiry"] >= today
    ]
    cache.update({"date": today, "instruments": nifty})
    return nifty


def _resolve_expiry(instruments: list) -> tuple:
    """Return (expiry_date, nse_fmt_str). Skips today if today is an expiry day."""
    today = datetime.now().date()
    expiries = sorted(set(i["expiry"] for i in instruments))
    for exp in expiries:
        if exp == today:
            continue      # never trade same-day expiry options
        if exp > today:
            return exp, exp.strftime("%d-%b-%Y")
    return expiries[0], expiries[0].strftime("%d-%b-%Y")


def _fetch_option_chain_from_kite(kite) -> Optional[OptionChainData]:
    try:
        spot = float(kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"])
        atm  = round(spot / 50) * 50

        nifty_insts  = _get_nifty_instruments(kite)
        expiry_date, nse_expiry = _resolve_expiry(nifty_insts)

        # Build strike→symbol map from the instruments list (avoids format guessing)
        symbol_map: dict[tuple, str] = {}
        for i in nifty_insts:
            if i["expiry"] != expiry_date:
                continue
            symbol_map[(int(i["strike"]), i["instrument_type"])] = f"NFO:{i['tradingsymbol']}"

        strikes = [atm + n * 50 for n in range(-15, 16)]
        to_quote = [
            symbol_map[(s, t)]
            for s in strikes for t in ("CE", "PE")
            if (s, t) in symbol_map
        ]

        if not to_quote:
            logger.error("Kite OC: no matching symbols in instruments list")
            return None

        quotes = kite.quote(to_quote)

        call_data: dict[int, StrikeData] = {}
        put_data:  dict[int, StrikeData] = {}
        new_snapshot: dict = {}

        for strike in strikes:
            for opt_type, store in (("CE", call_data), ("PE", put_data)):
                sym = symbol_map.get((strike, opt_type))
                if not sym or sym not in quotes:
                    continue
                q = quotes[sym]
                current_oi = q.get("oi", 0)
                prev_oi    = _oi_snapshot.get(sym, current_oi)
                change_oi  = current_oi - prev_oi
                new_snapshot[sym] = current_oi
                store[strike] = StrikeData(
                    strike        = strike,
                    oi            = current_oi,
                    change_oi     = change_oi,
                    pct_change_oi = round(change_oi / prev_oi * 100, 2) if prev_oi else 0,
                    ltp           = q.get("last_price", 0),
                    iv            = 0,
                    volume        = q.get("volume", 0),
                )

        _oi_snapshot.update(new_snapshot)

        if not call_data or not put_data:
            logger.error("Kite OC: no quotes returned")
            return None

        total_call_oi  = sum(s.oi for s in call_data.values())
        total_put_oi   = sum(s.oi for s in put_data.values())
        total_call_coi = sum(max(s.change_oi, 0) for s in call_data.values())
        total_put_coi  = sum(max(s.change_oi, 0) for s in put_data.values())

        pcr        = round(total_put_oi  / total_call_oi,  2) if total_call_oi  else 0
        change_pcr = round(total_put_coi / total_call_coi, 2) if total_call_coi else 0

        call_wall = max(call_data, key=lambda s: call_data[s].oi, default=0)
        put_wall  = max(put_data,  key=lambda s: put_data[s].oi,  default=0)
        max_pain  = _calculate_max_pain(
            {s: d.oi for s, d in call_data.items()},
            {s: d.oi for s, d in put_data.items()},
        )

        top_2_calls = sorted(
            [s for s in call_data.values() if s.ltp >= TRADEABLE_LTP_MIN],
            key=lambda s: s.oi, reverse=True
        )[:2]
        top_2_puts = sorted(
            [s for s in put_data.values() if s.ltp >= TRADEABLE_LTP_MIN],
            key=lambda s: s.oi, reverse=True
        )[:2]

        logger.info(
            f"[KITE OC] spot={spot} expiry={nse_expiry} pcr={pcr} "
            f"call_wall={call_wall} put_wall={put_wall} max_pain={max_pain} "
            f"top_calls={[s.strike for s in top_2_calls]} "
            f"top_puts={[s.strike for s in top_2_puts]}"
        )

        return OptionChainData(
            spot_price=spot, pcr=pcr, change_pcr=change_pcr,
            max_pain=max_pain, call_wall=call_wall, put_wall=put_wall,
            atm_strike=atm, weekly_expiry_date=nse_expiry,
            call_data=call_data, put_data=put_data,
            top_2_calls=top_2_calls, top_2_puts=top_2_puts,
        )

    except Exception as e:
        logger.exception(f"Kite option chain failed: {e}")
        return None
