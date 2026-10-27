"""Shared fake Angel server and client factory for the offline tests. Response shapes come from Angel's SDK source and
public docs, NOT from a live account (see test_angel_adapter.py)."""
import json
import math
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from broker import angel as A
from broker.angel import AngelClient, AngelError, AngelSessionError
from broker.angel_instruments import InstrumentTable, reduce_master, to_kite, parse_expiry
from broker.angel_paper import PaperLedger

# ───────────────────────── fakes ─────────────────────────
TODAY = date.today()
EXP1, EXP2 = TODAY + timedelta(days=6), TODAY + timedelta(days=13)
SPOT = 22800.0


def angel_exp(d):
    return d.strftime("%d%b%Y").upper()


def make_rows():
    """Master rows in Angel's REAL format (verified against the live public file)."""
    rows, tok = [], 50000
    for exp in (EXP1, EXP2):
        for k in range(21000, 24501, 50):
            for t in ("CE", "PE"):
                tok += 1
                rows.append({"token": str(tok), "symbol": f"NIFTY{exp.strftime('%d%b%y').upper()}{k}{t}", "name": "NIFTY",
                             "expiry": angel_exp(exp), "strike": f"{k * 100}.000000", "lotsize": "65", "instrumenttype": "OPTIDX",
                             "exch_seg": "NFO", "tick_size": "5.000000", "freeze_qty": "3511"})
    rows += [
        {"token": "51000", "symbol": "NIFTY26NOVFUT", "name": "NIFTY", "expiry": angel_exp(EXP2), "strike": "-1.000000", "lotsize": "65",
         "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "5.000000", "freeze_qty": "3511"},
        {"token": "60001", "symbol": "RELIANCE27OCT261400CE", "name": "RELIANCE", "expiry": angel_exp(EXP1), "strike": "140000.000000", "lotsize": "500",
         "instrumenttype": "OPTSTK", "exch_seg": "NFO", "tick_size": "5.000000", "freeze_qty": "5000"},
        {"token": "60002", "symbol": "NIFTYNXT5027OCT2650000CE", "name": "NIFTYNXT50", "expiry": angel_exp(EXP1), "strike": "5000000.000000", "lotsize": "25",
         "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000", "freeze_qty": "1000"},
        {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "0.000000", "lotsize": "1",
         "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000", "freeze_qty": "0"},
    ]
    return rows


ROWS = make_rows()
BY_TOKEN = {r["token"]: r for r in ROWS}
BY_SYMBOL = {r["symbol"]: r for r in ROWS}


def sym(exp, strike, t):
    return f"NIFTY{exp.strftime('%d%b%y').upper()}{strike}{t}"


def opt_price(r, spot):
    k = float(r["strike"]) / 100
    ce = r["symbol"].endswith("CE")
    intrinsic = max(spot - k, 0) if ce else max(k - spot, 0)
    return round((intrinsic + max(4.0, 140 * math.exp(-abs(k - spot) / 230))) / 0.05) * 0.05


class Resp:
    def __init__(self, body, status=200, text=None):
        self.status_code, self._b = status, body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._b is None:
            raise ValueError("not json")
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeAngel:
    """Stands in for apiconnect.angelone.in."""
    def __init__(self, spot=SPOT, max_batch=100, drop_silently=False, oi=True):
        self.spot, self.max_batch, self.drop_silently, self.oi = spot, max_batch, drop_silently, oi
        self.calls, self.orders, self.positions_raw = [], [], []
        self.fail_place = None
        self.rate_limit_n = 0
        self.net_error_n = 0
        self.seq = 0

    def market(self, token):
        if token == "99926000":
            return {"ltp": self.spot}
        r = BY_TOKEN.get(token)
        if not r:
            return None
        return {"ltp": opt_price(r, self.spot), "oi": (int(200000 - abs(float(r["strike"]) / 100 - self.spot) * 150) if self.oi else 0), "vol": 1234}

    def request(self, method, url, headers=None, timeout=None, data=None):
        route = url.replace(A.ROOT_URL, "")
        payload = json.loads(data) if data else {}
        self.calls.append((route, payload, method, headers or {}))
        if self.net_error_n:
            self.net_error_n -= 1
            raise requests.ConnectionError("boom")
        if self.rate_limit_n:
            self.rate_limit_n -= 1
            return Resp(None, 403, "Access denied because of exceeding access rate")
        ok = lambda data=None: Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": data})
        bad = lambda msg, code="AB1000": Resp({"status": False, "message": msg, "errorcode": code, "data": None})
        if route.endswith("loginByPassword"):
            if payload.get("password") != "1234":
                return bad("Invalid credentials", "AB1007")
            return ok({"jwtToken": "JWT-ABC", "refreshToken": "REF", "feedToken": "FEED"})
        if route.endswith("getProfile"):
            return ok({"clientcode": "A123456", "name": "Test", "exchanges": ["NSE", "NFO"]})
        if route.endswith("getRMS"):
            return ok({"net": "500000.00", "availablecash": "450000.00"})
        if route.endswith("/quote"):
            exch, toks = next(iter(payload["exchangeTokens"].items()))
            if len(toks) > self.max_batch:
                if self.drop_silently:
                    toks = toks[:self.max_batch]
                else:
                    return bad("Too many tokens in one request", "AB1004")
            fetched, unf = [], []
            for t in toks:
                m = self.market(t)
                if m is None:
                    unf.append({"exchange": exch, "symbolToken": t, "message": "no data", "errorCode": "x"})
                    continue
                e = {"exchange": exch, "tradingSymbol": (BY_TOKEN.get(t) or {}).get("symbol", "Nifty 50"), "symbolToken": t, "ltp": m["ltp"]}
                if payload["mode"] == "FULL":
                    e.update({"open": m["ltp"], "high": m["ltp"], "low": m["ltp"], "close": m["ltp"], "tradeVolume": m.get("vol", 0), "opnInterest": m.get("oi", 0), "netChange": 0.0})
                fetched.append(e)
            return ok({"fetched": fetched, "unfetched": unf})
        if route.endswith("getLtpData"):
            m = self.market(payload["symboltoken"])
            return ok({"exchange": payload["exchange"], "tradingsymbol": payload["tradingsymbol"], "symboltoken": payload["symboltoken"], "ltp": m["ltp"]}) if m else bad("no data")
        if route.endswith("getCandleData"):
            base = datetime(2026, 10, 27, 9, 15)
            rows = [[(base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%S+0530"), self.spot - 10 + i, self.spot + 5 + i, self.spot - 15 + i, self.spot - 5 + i, 0] for i in range(25)]
            return ok(rows)
        if route.endswith("getOrderBook"):
            return ok(self.orders)
        if route.endswith("getPosition"):
            return ok(self.positions_raw)
        if route.endswith("placeOrder"):
            if self.fail_place:
                return bad(self.fail_place, "AB4036")
            self.seq += 1
            oid = f"2610{self.seq:06d}"
            m = self.market(payload["symboltoken"])
            px = float(payload["price"])
            fill = payload["ordertype"] == "MARKET" or (payload["transactiontype"] == "BUY" and px >= m["ltp"]) or (payload["transactiontype"] == "SELL" and px <= m["ltp"])
            self.orders.append({
                "variety": "NORMAL", "ordertype": payload["ordertype"], "producttype": payload["producttype"], "duration": "DAY", "price": px,
                "quantity": payload["quantity"], "tradingsymbol": payload["tradingsymbol"], "transactiontype": payload["transactiontype"],
                "exchange": payload["exchange"], "symboltoken": payload["symboltoken"], "averageprice": m["ltp"] if fill else 0.0,
                "filledshares": payload["quantity"] if fill else "0", "unfilledshares": "0" if fill else payload["quantity"], "orderid": oid,
                "text": "", "status": "complete" if fill else "open", "orderstatus": "complete" if fill else "open",
                "updatetime": datetime.now().strftime("%d-%b-%Y %H:%M:%S"), "exchtime": datetime.now().strftime("%d-%b-%Y %H:%M:%S")})
            return ok({"orderid": oid, "uniqueorderid": "u" + oid})
        if route.endswith("modifyOrder"):
            o = next((x for x in self.orders if x["orderid"] == payload["orderid"]), None)
            if not o:
                return bad("Order not found")
            o["price"] = float(payload["price"])
            return ok({"orderid": o["orderid"]})
        if route.endswith("cancelOrder"):
            o = next((x for x in self.orders if x["orderid"] == payload["orderid"]), None)
            if not o:
                return bad("Order not found")
            o["status"] = o["orderstatus"] = "cancelled"
            return ok({"orderid": o["orderid"]})
        return bad(f"unknown route {route}")


def make_client(fake=None, paper=False, home=None, **kw):
    home = Path(home or tempfile.mkdtemp())
    fake = fake or FakeAngel()
    tbl = InstrumentTable(home / "data" / "angel_instruments.json", http=type("H", (), {"get": staticmethod(lambda url, timeout=0: Resp(ROWS))})())
    c = AngelClient("APIKEY", "JWT-ABC", home=home, session=fake, instruments=tbl, paper=paper, public_ip="203.0.113.9",
                    min_interval=0, sleep=lambda s: None, **kw)
    return c, fake, home
