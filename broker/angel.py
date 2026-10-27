"""
Angel One (SmartAPI) client with the same surface the bot already uses on Zerodha's KiteConnect:

    positions()['net'], orders(), ltp(), quote(), place_order(), modify_order(), instruments(),
    historical_data(), and the VARIETY_/EXCHANGE_/TRANSACTION_TYPE_/ORDER_TYPE_ constants.

So main.py, order_manager.py and the option-chain code run unchanged on either broker.

Written against Angel's SDK source (routes, headers, login flow) and its README (order/candle fields).
I did NOT use the SDK itself: it makes a network call at import, creates a logs/ folder in the working
directory and — via a `finally:` block — always overwrites the public-IP header with a hard-coded
address, which is wrong on a server whose IP is registered with the broker.

WHAT IS NOT YET VERIFIED against a live Angel account (run scripts/verify_angel.py to check each):
  * response field names of the order book / positions / quotes (parsed defensively, see below)
  * how many tokens one quote request may carry and the data rate limit (the adapter adapts at runtime)
  * whether option quotes carry open interest
  * real order placement / modify / cancel
Until then Angel runs in PAPER mode by default: real market data, simulated fills (broker/angel_paper.py).
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests

from broker.angel_instruments import ANGEL_NIFTY_INDEX, KITE_NIFTY_INDEX_TOKEN, InstrumentTable
from broker.angel_paper import PaperLedger

logger = logging.getLogger(__name__)

ROOT_URL = "https://apiconnect.angelone.in"
ROUTES = {
    "login":    "/rest/auth/angelbroking/user/v1/loginByPassword",
    "profile":  "/rest/secure/angelbroking/user/v1/getProfile",
    "rms":      "/rest/secure/angelbroking/user/v1/getRMS",
    "place":    "/rest/secure/angelbroking/order/v1/placeOrder",
    "modify":   "/rest/secure/angelbroking/order/v1/modifyOrder",
    "cancel":   "/rest/secure/angelbroking/order/v1/cancelOrder",
    "orders":   "/rest/secure/angelbroking/order/v1/getOrderBook",
    "position": "/rest/secure/angelbroking/order/v1/getPosition",
    "ltp":      "/rest/secure/angelbroking/order/v1/getLtpData",
    "candles":  "/rest/secure/angelbroking/historical/v1/getCandleData",
    "quote":    "/rest/secure/angelbroking/market/v1/quote",
}
SESSION_ERROR_CODES = {"AG8001", "AG8002", "AG8003"}          # invalid / expired / missing token
INTERVALS = {"minute": "ONE_MINUTE", "3minute": "THREE_MINUTE", "5minute": "FIVE_MINUTE", "10minute": "TEN_MINUTE",
             "15minute": "FIFTEEN_MINUTE", "30minute": "THIRTY_MINUTE", "60minute": "ONE_HOUR", "day": "ONE_DAY"}
PRODUCT_TO_ANGEL = {"NRML": "CARRYFORWARD", "MIS": "INTRADAY", "CNC": "DELIVERY"}
PRODUCT_FROM_ANGEL = {v: k for k, v in PRODUCT_TO_ANGEL.items()}


class AngelError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class AngelSessionError(AngelError):
    """The login token is missing, invalid or expired — log in again."""


def _num(x, default=0.0) -> float:
    try:
        return float(str(x).replace(",", "").replace(" ", "")) if x not in (None, "") else default
    except ValueError:
        return default


def _int(x, default=0) -> int:
    return int(_num(x, default))


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _parse_time(*candidates) -> datetime:
    """Angel order times look like '28-Oct-2026 10:30:05'. Try every field we were given; fall back to now."""
    for s in candidates:
        if not s:
            continue
        for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %I:%M:%S %p"):
            try:
                return datetime.strptime(str(s).strip(), fmt)
            except ValueError:
                continue
    return datetime.now()


def map_status(raw: str) -> str:
    """Angel's order status text -> the Kite status the bot's safety checks understand."""
    s = (raw or "").strip().lower()
    if s in ("complete", "completed", "executed"):
        return "COMPLETE"
    if s in ("rejected",):
        return "REJECTED"
    if s in ("cancelled", "canceled"):
        return "CANCELLED"
    if s == "trigger pending":
        return "TRIGGER PENDING"
    return "OPEN"                       # open, open pending, validation pending, modified, put order req received…


class AngelClient:
    VARIETY_REGULAR = "regular"
    EXCHANGE_NFO = "NFO"
    EXCHANGE_NSE = "NSE"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    PRODUCT_NRML = "NRML"
    PRODUCT_MIS = "MIS"

    def __init__(self, api_key, access_token, *, home: Path | None = None, session=None, instruments: InstrumentTable | None = None,
                 paper: bool = True, ledger: PaperLedger | None = None, public_ip=None, local_ip=None, mac=None,
                 timeout=10, min_interval=None, quote_batch=None, sleep=time.sleep, clock=time.monotonic):
        import paths
        home = Path(home) if home else paths.HOME
        self.api_key = api_key
        self.access_token = access_token
        self.http = session or requests.Session()
        self.paper = paper
        self.instruments_table = instruments or InstrumentTable(home / "data" / "angel_instruments.json")
        self.ledger = ledger if ledger is not None else (PaperLedger(home / "paper_ledger.json") if paper else None)
        self._public_ip = public_ip or os.environ.get("ANGEL_PUBLIC_IP") or None
        self._local_ip = local_ip or os.environ.get("ANGEL_LOCAL_IP") or "127.0.0.1"
        self._mac = mac or ":".join(re.findall("..", "%012x" % uuid.getnode()))
        self.timeout = timeout
        self.min_interval = float(min_interval if min_interval is not None else os.environ.get("ANGEL_MIN_INTERVAL", 0.4))
        self.quote_batch = int(quote_batch if quote_batch is not None else os.environ.get("ANGEL_QUOTE_BATCH", 50))
        self._sleep, self._clock = sleep, clock
        self._last_call = None
        self._oi_warned = False
        self.stats = {"calls": 0, "retries": 0}

    # ───────────────────────── HTTP ─────────────────────────
    def public_ip(self) -> str:
        if self._public_ip is None:
            try:
                self._public_ip = requests.get("https://api.ipify.org", timeout=3).text.strip()
            except Exception:
                self._public_ip = "127.0.0.1"
        return self._public_ip

    def _headers(self, auth=True) -> dict:
        h = {"Content-type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": self._local_ip, "X-ClientPublicIP": self.public_ip(), "X-MACAddress": self._mac,
             "X-PrivateKey": self.api_key}
        if auth and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _throttle(self):
        if self._last_call is not None:
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _call(self, route: str, payload=None, method="POST", retries=3, auth=True) -> dict:
        url = ROOT_URL + ROUTES[route]
        last = None
        for attempt in range(retries):
            self._throttle()
            self.stats["calls"] += 1
            try:
                r = self.http.request(method, url, headers=self._headers(auth), timeout=self.timeout,
                                      data=json.dumps(payload) if payload is not None else None)
            except requests.RequestException as e:
                last = AngelError(f"network error calling {route}: {e}")
                self.stats["retries"] += 1
                self._sleep(0.5 * (attempt + 1))
                continue
            text = getattr(r, "text", "") or ""
            if r.status_code == 429 or "exceeding access rate" in text.lower():
                last = AngelError(f"Angel rate limit hit on {route}", "RATE")
                self.stats["retries"] += 1
                self._sleep(1.0 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise AngelSessionError("Angel rejected the login token (HTTP 401) — log in again.", "401")
            try:
                body = r.json()
            except ValueError:
                raise AngelError(f"Angel returned a non-JSON reply for {route} (HTTP {r.status_code}).")
            if body.get("status") in (True, "true", "True"):
                return body
            code, msg = body.get("errorcode") or "", body.get("message") or "unknown error"
            if code in SESSION_ERROR_CODES or re.search(r"invalid token|token (is )?expired|session expired", msg, re.I):
                raise AngelSessionError(msg, code)
            raise AngelError(msg, code)
        raise last or AngelError(f"{route} failed")

    # ───────────────────────── instruments / resolution ─────────────────────────
    def instruments(self, exchange=None) -> list:
        """Kite-shaped NIFTY option/future records (the only instruments this bot trades)."""
        return self.instruments_table.kite_instruments()

    def _resolve(self, key: str):
        r = self.instruments_table.resolve(key)
        if r is None:
            logger.warning(f"Angel: unknown instrument {key!r} (instrument list may be stale)")
        return r

    def _ltp_of_symbol(self, symbol: str):
        try:
            return self.ltp([f"NFO:{symbol}"]).get(f"NFO:{symbol}", {}).get("last_price")
        except AngelError:
            return None

    # ───────────────────────── quotes ─────────────────────────
    def _market(self, mode: str, exchange: str, tokens: list) -> dict:
        """token -> raw entry. Batch size adapts: if Angel silently drops tokens or errors, go to smaller batches."""
        got: dict = {}
        pending = list(tokens)
        while pending:
            size = max(1, self.quote_batch)
            chunk, pending = pending[:size], pending[size:]
            try:
                body = self._call("quote", {"mode": mode, "exchangeTokens": {exchange: chunk}})
            except AngelSessionError:
                raise
            except AngelError as e:
                if size > 1:
                    self.quote_batch = max(1, size // 2)
                    logger.warning(f"Angel quote batch of {size} failed ({e}); trying {self.quote_batch}.")
                    pending = chunk + pending
                    continue
                logger.warning(f"Angel quote failed for token {chunk}: {e}")
                continue
            data = body.get("data") or {}
            fetched = data.get("fetched") or []
            for e in fetched:
                got[str(e.get("symbolToken"))] = e
            missing = [t for t in chunk if t not in got]
            if missing and size > 1 and not data.get("unfetched"):
                self.quote_batch = 1              # tokens vanished with no explanation: this account takes one at a time
                logger.warning("Angel dropped tokens from a multi-token quote — switching to one token per request.")
                pending = missing + pending
        return got

    def ltp(self, keys) -> dict:
        keys = [keys] if isinstance(keys, str) else list(keys)
        out = {}
        by_exch: dict = {}
        for k in keys:
            r = self._resolve(k)
            if r:
                by_exch.setdefault(r[0], []).append((k, r[1], r[2]))
        for exch, items in by_exch.items():
            got = self._market("LTP", exch, [t for _, _, t in items])
            for key, sym, tok in items:
                e = got.get(tok)
                if e is None:                      # last resort: the single-symbol endpoint
                    try:
                        d = self._call("ltp", {"exchange": exch, "tradingsymbol": sym, "symboltoken": tok}).get("data") or {}
                        e = {"ltp": d.get("ltp")}
                    except AngelError:
                        continue
                out[key] = {"instrument_token": _int(tok), "last_price": _num(e.get("ltp"))}
        return out

    def quote(self, keys) -> dict:
        keys = [keys] if isinstance(keys, str) else list(keys)
        out = {}
        by_exch: dict = {}
        for k in keys:
            r = self._resolve(k)
            if r:
                by_exch.setdefault(r[0], []).append((k, r[1], r[2]))
        n_opts = n_oi = 0
        for exch, items in by_exch.items():
            got = self._market("FULL", exch, [t for _, _, t in items])
            for key, sym, tok in items:
                e = got.get(tok)
                if e is None:
                    continue
                oi = _num(e.get("opnInterest"))
                if exch == "NFO":
                    n_opts += 1
                    n_oi += 1 if oi else 0
                out[key] = {
                    "instrument_token": _int(tok), "last_price": _num(e.get("ltp")), "oi": oi,
                    "volume": _int(e.get("tradeVolume")),
                    "ohlc": {"open": _num(e.get("open")), "high": _num(e.get("high")), "low": _num(e.get("low")), "close": _num(e.get("close"))},
                    "net_change": _num(e.get("netChange")),
                }
        if n_opts >= 10 and n_oi == 0 and not self._oi_warned:
            self._oi_warned = True
            logger.error("Angel returned ZERO open interest for every option quoted — the OI-based signals (PCR, call/put "
                         "walls, writing) cannot work on this feed. Run scripts/verify_angel.py.")
        return out

    # ───────────────────────── history ─────────────────────────
    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False) -> list:
        tok = int(instrument_token)
        if tok == KITE_NIFTY_INDEX_TOKEN:
            exch, token = ANGEL_NIFTY_INDEX
        else:
            exch, token = "NFO", str(tok)
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M") if hasattr(d, "strftime") else str(d)[:16]
        if interval not in INTERVALS:
            raise AngelError(f"Unsupported candle interval {interval!r}.")
        body = self._call("candles", {"exchange": exch, "symboltoken": token, "interval": INTERVALS[interval],
                                      "fromdate": fmt(from_date), "todate": fmt(to_date)})
        out = []
        for row in body.get("data") or []:
            ts = row[0]
            try:
                when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")       # '2026-10-28T09:15:00+0530' (tz-aware, like Kite)
            except ValueError:
                when = datetime.fromisoformat(ts)
            out.append({"date": when, "open": _num(row[1]), "high": _num(row[2]), "low": _num(row[3]),
                        "close": _num(row[4]), "volume": _int(row[5]) if len(row) > 5 else 0})
        return out

    # ───────────────────────── positions / orders ─────────────────────────
    def positions(self) -> dict:
        if self.paper:
            self.ledger.settle(self._ltp_of_symbol)
            net = self.ledger.positions()
            return {"net": net, "day": net}
        body = self._call("position", method="GET")
        net = []
        for p in body.get("data") or []:
            buy, sell = _int(p.get("buyqty")), _int(p.get("sellqty"))
            nq = p.get("netqty")
            qty = _int(nq) if nq not in (None, "") else buy - sell
            if nq not in (None, "") and (buy or sell) and qty != buy - sell and abs(qty) == abs(buy - sell):
                # netqty is exactly the opposite of buy-sell: the sign convention is not what we assumed. Short positions
                # drive stops and reconciliation, so trust the arithmetic and shout.
                self.stats["sign_flips"] = self.stats.get("sign_flips", 0) + 1
                logger.error(f"Angel position {p.get('tradingsymbol')}: netqty {qty} contradicts buy {buy} - sell {sell}; using {buy - sell}.")
                qty = buy - sell
            net.append({
                "tradingsymbol": p.get("tradingsymbol"), "exchange": p.get("exchange"),
                "product": PRODUCT_FROM_ANGEL.get(p.get("producttype"), p.get("producttype")),
                "instrument_token": _int(p.get("symboltoken")), "quantity": qty,
                "buy_quantity": buy, "sell_quantity": sell,
                "buy_price": _num(p.get("buyavgprice")), "sell_price": _num(p.get("sellavgprice")),
                "average_price": _num(p.get("avgnetprice")), "last_price": _num(p.get("ltp")),
                "pnl": _num(p.get("pnl", p.get("unrealised", 0))),
            })
        return {"net": net, "day": net}

    def _raw_orders(self) -> list:
        return self._call("orders", method="GET").get("data") or []

    def orders(self) -> list:
        if self.paper:
            self.ledger.settle(self._ltp_of_symbol)
            return self.ledger.order_book()
        out = []
        for o in self._raw_orders():
            qty, filled = _int(o.get("quantity")), _int(o.get("filledshares", o.get("filledquantity")))
            out.append({
                "order_id": str(o.get("orderid")), "tradingsymbol": o.get("tradingsymbol"),
                "exchange": o.get("exchange"), "transaction_type": (o.get("transactiontype") or "").upper(),
                "status": map_status(o.get("orderstatus") or o.get("status")),
                "status_message": o.get("text") or None, "quantity": qty, "filled_quantity": filled,
                "pending_quantity": _int(o.get("unfilledshares"), qty - filled), "price": _num(o.get("price")),
                "average_price": _num(o.get("averageprice")), "order_type": o.get("ordertype"),
                "product": PRODUCT_FROM_ANGEL.get(o.get("producttype"), o.get("producttype")),
                "variety": "regular", "order_timestamp": _parse_time(o.get("updatetime"), o.get("exchtime"), o.get("exchorderupdatetime")),
            })
        return out

    def place_order(self, variety=None, exchange=None, tradingsymbol=None, transaction_type=None, quantity=None,
                    product=None, order_type=None, price=None, validity=None, trigger_price=None, tag=None, **_):
        exchange = exchange or self.EXCHANGE_NFO
        r = self.instruments_table.resolve(f"{exchange}:{tradingsymbol}")
        if r is None:
            raise AngelError(f"Unknown instrument {tradingsymbol} — cannot place the order (instrument list may be stale).")
        order_type = order_type or self.ORDER_TYPE_LIMIT
        if order_type == self.ORDER_TYPE_LIMIT and price is None:
            raise AngelError("A LIMIT order needs a price.")
        if self.paper:
            oid = self.ledger.place(exchange, tradingsymbol, transaction_type, quantity, price, order_type, product or "NRML",
                                    self._ltp_of_symbol(tradingsymbol))
            logger.info(f"[PAPER] {transaction_type} {tradingsymbol} qty={quantity} price={price} -> {oid}")
            return oid
        body = self._call("place", {
            "variety": "NORMAL", "tradingsymbol": tradingsymbol, "symboltoken": r[2], "transactiontype": transaction_type,
            "exchange": exchange, "ordertype": order_type, "producttype": PRODUCT_TO_ANGEL.get(product or "NRML", product),
            "duration": "DAY", "price": "0" if order_type == self.ORDER_TYPE_MARKET else str(price),
            "squareoff": "0", "stoploss": "0", "quantity": str(int(quantity)),
        })
        oid = (body.get("data") or {}).get("orderid")
        if not oid:
            raise AngelError(f"Angel accepted the request but returned no order id: {body}")
        return str(oid)

    def modify_order(self, variety=None, order_id=None, quantity=None, price=None, order_type=None, trigger_price=None, validity=None, **_):
        if self.paper:
            self.ledger.modify(order_id, price=price, quantity=quantity,
                               ltp=self._ltp_of_symbol((self.ledger.get(order_id) or {}).get("tradingsymbol", "")))
            return order_id
        raw = next((o for o in self._raw_orders() if str(o.get("orderid")) == str(order_id)), None)
        if raw is None:
            raise AngelError(f"Order {order_id} not found in the order book — cannot modify it.")
        body = self._call("modify", {
            "variety": raw.get("variety") or "NORMAL", "orderid": str(order_id),
            "ordertype": order_type or raw.get("ordertype"), "producttype": raw.get("producttype"),
            "duration": raw.get("duration") or "DAY", "price": str(price if price is not None else raw.get("price")),
            "quantity": str(int(quantity if quantity is not None else _int(raw.get("quantity")))),
            "tradingsymbol": raw.get("tradingsymbol"), "symboltoken": raw.get("symboltoken"), "exchange": raw.get("exchange"),
        })
        return str((body.get("data") or {}).get("orderid") or order_id)

    def cancel_order(self, variety=None, order_id=None, parent_order_id=None):
        if self.paper:
            self.ledger.cancel(order_id)
            return order_id
        raw = next((o for o in self._raw_orders() if str(o.get("orderid")) == str(order_id)), None)
        self._call("cancel", {"variety": (raw or {}).get("variety") or "NORMAL", "orderid": str(order_id)})
        return order_id

    # ───────────────────────── account ─────────────────────────
    def profile(self) -> dict:
        return self._call("profile", method="GET").get("data") or {}

    def rms(self) -> dict:
        return self._call("rms", method="GET").get("data") or {}
