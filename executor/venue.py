"""
Where orders go. One small interface, three implementations:

    BrokerVenue(client)   a Kite-shaped client (KiteConnect, or the repo's AngelClient) — real orders
    PaperVenue(quotes)    never sends an order; fills at the live price if `quotes` (a broker client)
                          is given, else at the instruction's reference price

    execute(side, symbol, qty, ref) -> Fill      confirmed fill (may be partial or zero)
    place_stop(symbol, qty, trigger) -> id|None  a broker BUY stop protecting a short (None = unsupported)
    cancel_stop(id)
    spot() -> float|None                         Nifty 50 index price

Real-order rules (learned on the owner's own bot):
  * exchanges reject MARKET orders for options sent via API → LIMIT orders priced just past the market;
  * "accepted" is not "filled" → the order book is polled until the fill is confirmed;
  * an unfilled order is re-priced (up to REPRICE times), then cancelled — never left working silently.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("executor.venue")

NIFTY_KEY = "NSE:NIFTY 50"
TICK = 0.05


@dataclass
class Fill:
    qty: int
    avg: Optional[float]
    note: str = ""


def tick(price: float, up: bool) -> float:
    n = price / TICK
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * TICK, 2)


def cross(ltp: float, side: str, step: int = 0) -> float:
    """A limit price that should fill now: past the last price by max(₹2, 1%), widening each re-price."""
    pad = max(2.0, 0.01 * ltp) * (1 + step)
    return tick(ltp + pad, up=True) if side == "BUY" else max(TICK, tick(ltp - pad, up=False))


class BrokerVenue:
    REPRICE = 3
    WAIT_SEC = 6.0
    POLL = 1.0

    def __init__(self, client, exchange: str = "NFO", product: str = "NRML", stops: bool = True,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.c, self.exchange, self.product = client, exchange, product
        self.stops_supported = stops
        self.sleep, self.clock = sleep, clock

    def ltp(self, symbol: str) -> Optional[float]:
        key = f"{self.exchange}:{symbol}"
        try:
            v = (self.c.ltp([key]).get(key) or {}).get("last_price")
            return float(v) if v else None
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"LTP {symbol}: {e}")
            return None

    def spot(self) -> Optional[float]:
        try:
            v = (self.c.ltp([NIFTY_KEY]).get(NIFTY_KEY) or {}).get("last_price")
            return float(v) if v else None
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"spot: {e}")
            return None

    def _order(self, oid: str) -> Optional[dict]:
        try:
            return next((o for o in self.c.orders() if str(o.get("order_id")) == str(oid)), None)
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"order book: {e}")
            return None

    def execute(self, side: str, symbol: str, qty: int, ref: Optional[float]) -> Fill:
        if qty <= 0:
            return Fill(0, None)
        price = self.ltp(symbol) or ref
        if not price:
            return Fill(0, None, "no price available")
        limit = cross(price, side)
        try:
            oid = self.c.place_order(variety="regular", exchange=self.exchange, tradingsymbol=symbol,
                                     transaction_type=side, quantity=qty, product=self.product,
                                     order_type="LIMIT", price=limit)
        except Exception as e:                               # noqa: BLE001
            return Fill(0, None, f"rejected: {e}")
        logger.info(f"{side} {symbol} x{qty} @ {limit} → order {oid}")
        filled, avg = 0, None
        for step in range(self.REPRICE + 1):
            deadline = self.clock() + self.WAIT_SEC
            o = None
            while self.clock() < deadline:
                o = self._order(oid)
                if o and o.get("status") in ("COMPLETE", "REJECTED", "CANCELLED"):
                    break
                self.sleep(self.POLL)
            o = o or self._order(oid) or {}
            filled = int(o.get("filled_quantity") or 0)
            avg = float(o["average_price"]) if o.get("average_price") else None
            if o.get("status") == "COMPLETE":
                return Fill(filled or qty, avg)
            if o.get("status") in ("REJECTED", "CANCELLED"):
                return Fill(filled, avg, f"{o.get('status').lower()}: {o.get('status_message') or ''}".strip())
            if step == self.REPRICE:
                break
            new = cross(self.ltp(symbol) or price, side, step + 1)
            try:
                self.c.modify_order(variety="regular", order_id=oid, price=new)
                logger.info(f"re-priced {oid} → {new}")
            except Exception as e:                           # noqa: BLE001
                logger.warning(f"re-price {oid}: {e}")
        try:
            self.c.cancel_order(variety="regular", order_id=oid)
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"cancel {oid}: {e}")
        o = self._order(oid) or {}
        filled = int(o.get("filled_quantity") or filled)
        return Fill(filled, avg, f"not fully filled after {self.REPRICE} re-prices; rest cancelled")

    def place_stop(self, symbol: str, qty: int, trigger: float) -> Optional[str]:
        if not self.stops_supported or qty <= 0:
            return None
        trig = tick(trigger, up=True)
        try:
            return str(self.c.place_order(variety="regular", exchange=self.exchange, tradingsymbol=symbol,
                                          transaction_type="BUY", quantity=qty, product=self.product,
                                          order_type="SL", trigger_price=trig, price=tick(trig * 1.05, up=True)))
        except Exception as e:                               # noqa: BLE001
            logger.error(f"broker stop for {symbol} not placed: {e}")
            return None

    def cancel_stop(self, oid: Optional[str]) -> None:
        if not oid:
            return
        try:
            self.c.cancel_order(variety="regular", order_id=oid)
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"cancel stop {oid}: {e}")


class PaperVenue:
    """Never sends an order. Prices from a broker client when given (real quotes), else the reference."""
    stops_supported = True

    def __init__(self, quotes=None, exchange: str = "NFO"):
        self.q = BrokerVenue(quotes, exchange) if quotes is not None else None
        self.n = 0
        self.stops: dict = {}

    def ltp(self, symbol: str) -> Optional[float]:
        return self.q.ltp(symbol) if self.q else None

    def spot(self) -> Optional[float]:
        return self.q.spot() if self.q else None

    def execute(self, side: str, symbol: str, qty: int, ref: Optional[float]) -> Fill:
        if qty <= 0:
            return Fill(0, None)
        px = self.ltp(symbol) or ref
        logger.info(f"[PAPER] {side} {symbol} x{qty} @ {px}")
        return Fill(qty, px, "paper")

    def place_stop(self, symbol: str, qty: int, trigger: float) -> Optional[str]:
        self.n += 1
        oid = f"PAPER-SL-{self.n}"
        self.stops[oid] = (symbol, qty, trigger)
        return oid

    def cancel_stop(self, oid: Optional[str]) -> None:
        self.stops.pop(oid, None)


# ── broker logins ────────────────────────────────────────────────────────────

def kite_client(creds: dict):
    """Zerodha: log in with user id, password and TOTP (no browser), return a KiteConnect client."""
    import pyotp
    import requests
    from kiteconnect import KiteConnect
    base = "https://kite.zerodha.com"
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": base + "/", "Origin": base,
                      "Content-Type": "application/x-www-form-urlencoded"})
    r = s.post(f"{base}/api/login", data={"user_id": creds["ZERODHA_USER_ID"], "password": creds["ZERODHA_PASSWORD"]},
               timeout=15).json()
    if r.get("status") != "success":
        raise RuntimeError(f"Zerodha login failed: {r.get('message')}")
    r = s.post(f"{base}/api/twofa", data={"user_id": creds["ZERODHA_USER_ID"], "request_id": r["data"]["request_id"],
                                          "twofa_value": pyotp.TOTP(creds["ZERODHA_TOTP_SECRET"]).now(),
                                          "twofa_type": "totp", "skip_session": ""}, timeout=15).json()
    if r.get("status") != "success":
        raise RuntimeError(f"Zerodha TOTP failed: {r.get('message')}")
    url, loc = f"{base}/connect/login?api_key={creds['KITE_API_KEY']}&v=3", ""
    for _ in range(10):
        try:
            resp = s.get(url, allow_redirects=False, timeout=15)
        except requests.exceptions.ConnectionError:
            break
        nxt = resp.headers.get("Location", "")
        if "request_token=" in nxt:
            loc = nxt
            break
        if not nxt:
            break
        url = nxt
    if "request_token=" not in loc:
        raise RuntimeError("Zerodha did not return a request token — is the API app's redirect URL set?")
    kite = KiteConnect(api_key=creds["KITE_API_KEY"])
    sess = kite.generate_session(loc.split("request_token=")[1].split("&")[0], api_secret=creds["KITE_API_SECRET"])
    kite.set_access_token(sess["access_token"])
    return kite


def angel_client(creds: dict, home):
    """Angel One SmartAPI: log in with client code, PIN and TOTP; returns the Kite-shaped AngelClient."""
    import pyotp
    from broker.angel import AngelClient, AngelError
    c = AngelClient(creds["ANGEL_API_KEY"], None, home=home, paper=False)
    body = c._call("login", {"clientcode": creds["ANGEL_CLIENT_CODE"], "password": creds["ANGEL_PASSWORD"],
                             "totp": pyotp.TOTP(creds["ANGEL_TOTP_SECRET"]).now()}, auth=False)
    jwt = (body.get("data") or {}).get("jwtToken")
    if not jwt:
        raise AngelError(f"Angel login returned no token: {body.get('message')}")
    c.access_token = jwt
    return c
