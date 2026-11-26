"""
    python -m executor check     settings, API key, broker login, a price — places no orders
    python -m executor run       the always-on loop (systemd: executor.service)
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, time as dtime

from executor import config
from executor.api import Api, ApiError
from executor.engine import Engine, now_ist
from executor.venue import BrokerVenue, PaperVenue, angel_client, kite_client

logger = logging.getLogger("executor")
DAY_START, DAY_END = dtime(9, 0), dtime(15, 35)


def _client(s):
    if s.broker == "zerodha":
        return kite_client(s.creds)
    if s.broker == "angel":
        return angel_client(s.creds, s.home)
    if s.quotes_token_file and s.creds.get("KITE_API_KEY"):
        return _quotes_only(s)                               # paper with real prices, no login of its own
    return None


def _quotes_only(s):
    """Paper mode with real prices from an EXISTING Zerodha session (today's token file), so testing never
    creates a second login. Read-only use: PaperVenue never sends an order."""
    import json
    from kiteconnect import KiteConnect
    tok = json.loads(open(s.quotes_token_file).read())
    if tok.get("date") != date.today().isoformat():
        raise RuntimeError(f"{s.quotes_token_file} is not from today yet (Zerodha login runs at 08:45)")
    k = KiteConnect(api_key=s.creds["KITE_API_KEY"])
    k.set_access_token(tok["access_token"])
    return k


def _venue(s, client):
    if not s.live:
        return PaperVenue(client)
    # Angel's adapter has no stop-loss orders yet: on Angel the spot guard is the only automatic stop
    return BrokerVenue(client, product=s.product, stops=(s.broker == "zerodha"))


def check() -> int:
    s = config.load()
    bad = s.problems()
    for p in bad:
        print(f"✗ {p}")
    if bad:
        return 1
    print(f"✓ settings ({s.broker}, {s.mode})")
    try:
        st = Api(s.api_url, s.api_pin, s.api_key).status()
        print(f"✓ executor API: plan {st['plan']}, status {st['status']}" + (f", paid until {st['paid_until'][:10]}" if st.get("paid_until") else ""))
    except ApiError as e:
        print(f"✗ executor API: {e}")
        return 1
    if s.broker != "paper":
        try:
            c = _client(s)
            spot = BrokerVenue(c).spot()
            print(f"✓ {s.broker} login works; Nifty {spot}")
        except Exception as e:                               # noqa: BLE001
            print(f"✗ {s.broker} login: {e}")
            return 1
    print("All good." + ("" if s.live else " (paper mode: no real orders will be placed)"))
    return 0


def run() -> int:
    s = config.load()
    s.home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(s.home / "executor.log")])
    bad = s.problems()
    if bad:
        logger.error("not starting: " + "; ".join(bad))
        return 1
    api = Api(s.api_url, s.api_pin, s.api_key)
    engine, day, backoff = None, None, s.poll_sec
    logger.info(f"executor up — {s.broker}, {s.mode.upper()}")
    while True:
        now = now_ist()
        try:
            holding = bool(engine and engine.st["positions"])
            if not (DAY_START <= now.time() < DAY_END) and not holding:
                time.sleep(30)
                continue
            if day != now.date() or engine is None:         # log in once per trading day
                client = _client(s)
                engine = Engine(s, _venue(s, client), api)
                st = engine.start()
                day = now.date()
                logger.info(f"day start {day}: plan {st['plan']}, status {st['status']}")
            engine.tick()
            backoff = s.poll_sec
        except ApiError as e:
            logger.warning(f"API: {e}")
            if engine and engine.st["positions"]:
                engine.guard(now_ist())                       # keep protecting open positions while the API is unreachable
                engine.save()
            backoff = min(backoff * 2, 30)
        except Exception as e:                               # noqa: BLE001 — never die mid-day
            logger.exception(f"loop: {e}")
            backoff = min(backoff * 2, 30)
        time.sleep(backoff)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return check()
    if cmd == "run":
        return run()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
