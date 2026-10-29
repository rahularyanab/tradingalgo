"""
Offline tests for the Angel One adapter. A fake Angel server stands in for the real one; the bot's OWN code
(order manager, option-chain builder, reconcile, candle fetcher, verify script) runs against it.

IMPORTANT: the fake's response shapes come from Angel's SDK source, README and my memory of its docs — NOT
from a live account. These tests prove the adapter is self-consistent and the bot works through it; only
scripts/verify_angel.py on a real account proves the shapes match. Run:  python3 tests/test_angel_adapter.py
"""
import importlib.util
import json
import logging
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("ALGO_USER_DIR", None)

import requests                                       # noqa: E402

from broker import angel as A                         # noqa: E402
from broker.angel import AngelClient, AngelError, AngelSessionError   # noqa: E402
from broker.angel_instruments import InstrumentTable, reduce_master, to_kite, parse_expiry, stream_reduce   # noqa: E402
from broker.angel_paper import PaperLedger            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def md_ok(text):
    if text.count("`") % 2:
        return False
    plain = re.sub(r"`[^`]*`", "", text)
    return plain.count("_") % 2 == 0 and plain.count("*") % 2 == 0


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    except Exception as e:                           # wrong type of error
        return None
    return None


from fakes_angel import ROWS, BY_SYMBOL, TODAY, EXP1, EXP2, SPOT, angel_exp, sym, opt_price, make_rows, Resp, FakeAngel, make_client  # noqa: E402

# ───────────────────────── tests ─────────────────────────
print("\n[1] instrument master (format verified against Angel's real public file)")
red = reduce_master(ROWS)
check("keeps NIFTY index options + futures only (drops stock options, NIFTYNXT50, the index row)",
      all(r["name"] == "NIFTY" and r["instrumenttype"] in ("OPTIDX", "FUTIDX") for r in red) and len(red) == len(ROWS) - 3, str(len(red)))
k = to_kite(next(r for r in ROWS if r["symbol"] == sym(EXP1, 22800, "CE")))
check("Kite-shaped: strike /100, expiry as a date, tick 0.05, lot 65, type CE",
      k["strike"] == 22800.0 and k["expiry"] == EXP1 and abs(k["tick_size"] - 0.05) < 1e-9 and k["lot_size"] == 65 and k["instrument_type"] == "CE" and k["name"] == "NIFTY", str(k))
check("future gets type FUT; junk rows are skipped", to_kite(BY_SYMBOL["NIFTY26NOVFUT"])["instrument_type"] == "FUT" and to_kite({"symbol": "X", "expiry": "", "token": "1"}) is None)
check("expiry parsing", parse_expiry("27OCT2026") == date(2026, 10, 27) and parse_expiry("") is None and parse_expiry("garbage") is None)
home = Path(tempfile.mkdtemp())
dl = []
tbl = InstrumentTable(home / "i.json", http=type("H", (), {"get": staticmethod(lambda url, timeout=0: dl.append(1) or Resp(ROWS))})())
n = len(tbl.kite_instruments())
tbl2 = InstrumentTable(home / "i.json", http=type("H", (), {"get": staticmethod(lambda url, timeout=0: dl.append(1) or Resp(ROWS))})())
tbl2.kite_instruments()
check("downloaded once a day, the cache serves the rest", len(dl) == 1 and n > 250, str((len(dl), n)))
tbl3 = InstrumentTable(home / "i.json", http=type("H", (), {"get": staticmethod(lambda url, timeout=0: (_ for _ in ()).throw(RuntimeError("offline")))})(), today=TODAY + timedelta(days=1))
check("next day with the download failing: falls back to the stale cache instead of having no instruments", len(tbl3.kite_instruments()) == n)
tbl4 = InstrumentTable(home / "none.json", http=type("H", (), {"get": staticmethod(lambda url, timeout=0: (_ for _ in ()).throw(RuntimeError("offline")))})())
check("no cache and no download → a clear error", raises(RuntimeError, tbl4.load) is not None)
blob = json.dumps(ROWS, ensure_ascii=False).encode()
same = True
for size in (1, 7, 64, 1000, len(blob) + 5):               # chunk boundaries everywhere, including inside multi-byte text and numbers
    chunks = [blob[i:i + size] for i in range(0, len(blob), size)]
    same = same and stream_reduce(chunks) == reduce_master(ROWS)
check("streaming the master in tiny chunks gives EXACTLY the same rows as loading it whole", same)
check("streaming: pretty-printed / newline-separated JSON also works", stream_reduce([json.dumps(ROWS, indent=2).encode()]) == reduce_master(ROWS))
check("streaming: an empty array and a non-array are handled", stream_reduce([b"[]"]) == [] and raises(ValueError, stream_reduce, [b'{"a":1}']) is not None)
check("resolve: index key, option symbol, unknown", tbl.resolve("NSE:NIFTY 50") == ("NSE", "Nifty 50", "99926000") and tbl.resolve(f"NFO:{sym(EXP1, 22800, 'PE')}")[0] == "NFO" and tbl.resolve("NFO:NOPE") is None)

print("\n[2] HTTP layer")
import importlib
real_get = requests.get
requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call at import!"))
try:
    importlib.reload(A)
    check("importing the adapter makes NO network call (Angel's own SDK does)", True)
finally:
    requests.get = real_get
from broker.angel import AngelClient, AngelError, AngelSessionError      # noqa: E402,F811  (reloaded)
c, fake, _ = make_client()
c.profile()
route, payload, method, h = fake.calls[-1]
check("headers: bearer token, API key, user type, real configured public IP (not a hard-coded one)",
      h["Authorization"] == "Bearer JWT-ABC" and h["X-PrivateKey"] == "APIKEY" and h["X-ClientPublicIP"] == "203.0.113.9" and h["X-UserType"] == "USER" and h["Content-type"] == "application/json", str(h))
check("GET requests carry no body", method == "GET" and payload == {})
e = raises(AngelError, c._call, "login", {"clientcode": "x", "password": "bad", "totp": "1"}, auth=False)
check("a failed call raises with Angel's message and code", e and "Invalid credentials" in str(e) and e.code == "AB1007", str(e))
for code in ("AG8001", "AG8002", "AG8003"):
    f = FakeAngel()
    f.request = lambda *a, _c=code, **k: Resp({"status": False, "message": "Invalid Token", "errorcode": _c})
    cc, _, _ = make_client(f)
    check(f"{code} raises AngelSessionError (log in again)", isinstance(raises(AngelSessionError, cc.profile), AngelSessionError))
f = FakeAngel(); f.request = lambda *a, **k: Resp(None, 401, "")
check("HTTP 401 raises AngelSessionError", raises(AngelSessionError, make_client(f)[0].profile) is not None)
f = FakeAngel(); f.request = lambda *a, **k: Resp(None, 502, "<html>bad gateway</html>")
check("a non-JSON reply is a clear error, not a crash", raises(AngelError, make_client(f)[0].profile) is not None)
c, fake, _ = make_client(); fake.rate_limit_n = 2
check("rate limiting is retried and succeeds", c.profile()["clientcode"] == "A123456" and c.stats["retries"] == 2, str(c.stats))
c, fake, _ = make_client(); fake.rate_limit_n = 5
e = raises(AngelError, c.profile)
check("persistent rate limiting gives up with a clear error", e is not None and "rate limit" in str(e).lower())
slept = []
c, fake, _ = make_client(); fake.rate_limit_n = 4; c._sleep = slept.append; c.min_interval = 0
rows = c.historical_data(256265, "2026-10-28 09:15", "2026-10-29 09:15", "15minute")
check("candles survive 4 refusals in a row (5 tries) and the waits are 3+6+9+12 s", len(rows) == 25 and [x for x in slept if x >= 1] == [3.0, 6.0, 9.0, 12.0], str(slept))
slept = []
c, fake, _ = make_client(); fake.rate_limit_n = 9; c._sleep = slept.append; c.min_interval = 0
e = raises(AngelError, c.historical_data, 256265, "2026-10-28 09:15", "2026-10-29 09:15", "15minute")
check("candles give up after 5 tries with the clear error, no sleep after the last", e is not None and "rate limit" in str(e).lower() and c.stats["calls"] == 5 and [x for x in slept if x >= 1] == [3.0, 6.0, 9.0, 12.0], str(slept))
slept = []
c, fake, _ = make_client(); fake.rate_limit_n = 9; c._sleep = slept.append; c.min_interval = 0
raises(AngelError, c.profile)
check("other routes keep the short 3-try retry", c.stats["calls"] == 3 and [x for x in slept if x >= 1] == [1.0, 2.0], str(slept))
c, fake, _ = make_client(); fake.net_error_n = 2
check("transient network errors are retried", c.profile()["name"] == "Test")
slept = []
c, fake, _ = make_client(); c.min_interval, c._sleep, t = 1.0, slept.append, [100.0]
c._clock = lambda: t[0]
c.profile(); t[0] += 0.3; c.profile()
check("calls are spaced by the minimum interval", slept and abs(slept[-1] - 0.7) < 1e-6, str(slept))

print("\n[3] quotes: batching, adaptation, open interest")
c, fake, _ = make_client()
keys = [f"NFO:{sym(EXP1, k, 'CE')}" for k in range(22000, 23600, 50)]       # 32 options
q = c.quote(keys)
check("one request carries the whole batch, every key returned under the caller's key", len(q) == 32 and sum(1 for r in fake.calls if r[0].endswith("/quote")) == 1)
x = q[keys[16]]
check("quote carries last_price / oi / volume as the option-chain builder expects", x["last_price"] > 0 and x["oi"] > 0 and x["volume"] == 1234, str(x))
check("ltp() works on the index with a plain string key too", c.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"] == SPOT)
check("an unknown symbol is skipped, not fatal", c.quote(keys[:2] + ["NFO:NOPE123"]).keys() == set(keys[:2]))
c, fake, _ = make_client(FakeAngel(max_batch=10))
q = c.quote(keys)
check("account that REJECTS big batches: halves until it works, returns everything", len(q) == 32 and c.quote_batch <= 10, f"{len(q)} {c.quote_batch}")
c, fake, _ = make_client(FakeAngel(max_batch=1, drop_silently=True))
q = c.quote(keys)
check("account that SILENTLY DROPS extra tokens: detected, falls to one-per-request, still returns everything",
      len(q) == 32 and c.quote_batch == 1, f"{len(q)} {c.quote_batch}")
c, fake, _ = make_client(FakeAngel(oi=False))
buf = []
h = logging.Handler(); h.emit = lambda r: buf.append(r.getMessage()); logging.getLogger("broker.angel").addHandler(h)
c.quote(keys); c.quote(keys)
check("zero open interest on every option is flagged loudly, once", sum("ZERO open interest" in m for m in buf) == 1, str(buf))

print("\n[4] candles + price, through the bot's own fetchers")
from data.market_data import fetch_nifty_candles, get_current_nifty_price      # noqa: E402
c, fake, _ = make_client()
df = fetch_nifty_candles(c)
check("fetch_nifty_candles works unchanged: columns, tz-aware IST index, 25 bars", list(df.columns) == ["open", "high", "low", "close", "volume"] and len(df) == 25 and str(df.index[0].tz) != "None", str(df.index[:1]))
req = [r for r in fake.calls if r[0].endswith("getCandleData")][0][1]
check("request uses the Nifty index token and FIFTEEN_MINUTE", req["symboltoken"] == "99926000" and req["exchange"] == "NSE" and req["interval"] == "FIFTEEN_MINUTE" and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", req["fromdate"]), str(req))
check("get_current_nifty_price works unchanged (single-string quote)", get_current_nifty_price(c) == SPOT)

print("\n[5] positions and order book (live shape → the bot's shape)")
c, fake, _ = make_client()
fake.positions_raw = [
    {"tradingsymbol": sym(EXP1, 22800, "CE"), "symboltoken": "x", "exchange": "NFO", "producttype": "CARRYFORWARD", "buyqty": "0", "sellqty": "130", "netqty": "-130", "avgnetprice": "80.5", "buyavgprice": "0", "sellavgprice": "80.5", "ltp": "70.0"},
    {"tradingsymbol": sym(EXP1, 23500, "CE"), "symboltoken": "y", "exchange": "NFO", "producttype": "CARRYFORWARD", "buyqty": "130", "sellqty": "0", "avgnetprice": "5.5", "buyavgprice": "5.5", "sellavgprice": "0"},
]
pos = c.positions()["net"]
check("short leg is negative, long positive, missing netqty falls back to buy−sell, product mapped back to NRML",
      pos[0]["quantity"] == -130 and pos[1]["quantity"] == 130 and pos[0]["product"] == "NRML" and pos[0]["average_price"] == 80.5, str(pos))
fake.orders = [
    {"orderid": "1", "tradingsymbol": "S1", "transactiontype": "BUY", "status": "open", "orderstatus": "open", "quantity": "130", "filledshares": "0", "unfilledshares": "130", "price": 4.5, "averageprice": 0.0, "updatetime": "28-Oct-2026 10:30:05", "variety": "NORMAL", "text": ""},
    {"orderid": "2", "tradingsymbol": "S2", "transactiontype": "SELL", "status": "complete", "orderstatus": "complete", "quantity": "65", "filledshares": "65", "unfilledshares": "0", "price": 100, "averageprice": 99.8, "updatetime": "28-Oct-2026 10:31:00", "variety": "NORMAL", "text": ""},
    {"orderid": "3", "tradingsymbol": "S3", "transactiontype": "BUY", "status": "rejected", "orderstatus": "rejected", "quantity": "65", "filledshares": "0", "unfilledshares": "65", "price": 100, "averageprice": 0, "updatetime": "28-Oct-2026 10:32:00", "variety": "NORMAL", "text": "RMS:Margin Exceeds"},
    {"orderid": "4", "tradingsymbol": "S4", "transactiontype": "BUY", "status": "cancelled", "quantity": "65", "filledshares": "0", "unfilledshares": "65", "price": 100, "averageprice": 0, "variety": "NORMAL"},
    {"orderid": "5", "tradingsymbol": "S5", "transactiontype": "BUY", "status": "trigger pending", "quantity": "65", "filledshares": "0", "unfilledshares": "65", "price": 100, "averageprice": 0, "variety": "NORMAL"},
    {"orderid": "6", "tradingsymbol": "S6", "transactiontype": "BUY", "status": "validation pending", "quantity": "65", "filledshares": "0", "unfilledshares": "65", "price": 100, "averageprice": 0, "variety": "NORMAL"},
]
od = {o["order_id"]: o for o in c.orders()}
check("statuses map to the ones the bot's safety checks look for",
      [od[i]["status"] for i in "123456"] == ["OPEN", "COMPLETE", "REJECTED", "CANCELLED", "TRIGGER PENDING", "OPEN"], str([od[i]["status"] for i in "123456"]))
check("quantities, prices, message and variety carried over; time is a naive datetime comparable to the bot's own",
      od["1"]["pending_quantity"] == 130 and od["2"]["average_price"] == 99.8 and od["3"]["status_message"] == "RMS:Margin Exceeds" and od["1"]["variety"] == "regular"
      and isinstance(od["1"]["order_timestamp"], datetime) and od["1"]["order_timestamp"].tzinfo is None and od["1"]["order_timestamp"] >= datetime(2026, 10, 28, 9, 0), str(od["1"]))

print("\n[6] placing orders through the bot's OWN order manager")
from execution import order_manager as om            # noqa: E402
c, fake, _ = make_client()
tgt = sym(EXP1, 22800, "CE"); hedge = sym(EXP1, 23700, "CE")
price = opt_price(BY_SYMBOL[tgt], SPOT)
r = om.place_sell_order(c, tgt, 130, price=price)
call = [x for x in fake.calls if x[0].endswith("placeOrder")][-1][1]
check("SELL goes out with Angel's field names and values (NORMAL / LIMIT / CARRYFORWARD / DAY, string qty, token)",
      r.success and call["variety"] == "NORMAL" and call["transactiontype"] == "SELL" and call["ordertype"] == "LIMIT" and call["producttype"] == "CARRYFORWARD"
      and call["duration"] == "DAY" and call["quantity"] == "130" and call["symboltoken"] == BY_SYMBOL[tgt]["token"] and call["exchange"] == "NFO", str(call))
check("the bot's −₹2 crossing buffer and ₹0.05 tick rounding survive", abs(float(call["price"]) - (math.floor((price - 2) / 0.05) * 0.05)) < 0.051, str(call["price"]))
fake.fail_place = "RMS:Margin Exceeds,Required:664000, Available:500000"
r = om.place_sell_order(c, tgt, 130, price=price)
check("a rejected order becomes OrderResult(success=False) carrying Angel's reason (the bot's alerts quote it)", (not r.success) and "Margin Exceeds" in r.error, str(r))
fake.fail_place = None
n_before = len(fake.calls)
e = raises(AngelError, c.place_order, exchange="NFO", tradingsymbol="NOPE", transaction_type="BUY", quantity=65, product="NRML", order_type="LIMIT", price=1)
check("an unknown symbol is refused before anything is sent", e is not None and len(fake.calls) == n_before and "Unknown instrument" in str(e), str((e, len(fake.calls) - n_before)))
check("a LIMIT order without a price is refused", raises(AngelError, c.place_order, exchange="NFO", tradingsymbol=tgt, transaction_type="BUY", quantity=65, product="NRML", order_type="LIMIT") is not None)
hp = om.place_spread_entry(c, tgt, hedge, 130, sell_price=price, hedge_price=opt_price(BY_SYMBOL[hedge], SPOT))
placed = [x[1]["tradingsymbol"] for x in fake.calls if x[0].endswith("placeOrder")][-2:]
check("spread entry buys the HEDGE first, then sells the main leg", placed == [hedge, tgt] and hp[0].success and hp[1].success, str(placed))

print("\n[7] modify and cancel (bot's chase code)")
c, fake, _ = make_client()
fake.max_batch = 100
r = om.place_buy_order(c, tgt, 65, price=1.0)                  # limit ≈ 3.0: below the market → rests
oid = r.order_id
book = c.orders()
mine = next(o for o in book if o["order_id"] == oid)
check("a resting order shows OPEN with all quantity pending", mine["status"] == "OPEN" and mine["pending_quantity"] == 65 and mine["variety"] == "regular", str(mine))
res = om.modify_order_price(c, mine, 150.0)
mp = [x for x in fake.calls if x[0].endswith("modifyOrder")][-1][1]
check("the bot's modify_order_price re-prices it in place, filling in Angel's required fields from the book",
      res.success and mp["orderid"] == oid and mp["variety"] == "NORMAL" and mp["producttype"] == "CARRYFORWARD" and mp["symboltoken"] == BY_SYMBOL[tgt]["token"] and float(mp["price"]) == 150.0 and mp["quantity"] == "65", str(mp))
check("…and Angel's book reflects the new price", next(o for o in c.orders() if o["order_id"] == oid)["price"] == 150.0)
check("modifying an order that isn't in the book is a clear error", raises(AngelError, c.modify_order, order_id="999999", price=1.0) is not None)
c.cancel_order(order_id=oid)
check("cancel works", next(o for o in c.orders() if o["order_id"] == oid)["status"] == "CANCELLED")

print("\n[8] PAPER mode: simulated orders, real data, consistent books")
home = Path(tempfile.mkdtemp())
c, fake, _ = make_client(paper=True, home=home)
r = om.place_spread_entry(c, tgt, hedge, 130, sell_price=price, hedge_price=opt_price(BY_SYMBOL[hedge], SPOT))
check("no order endpoint was ever called", not any(x[0].endswith(("placeOrder", "modifyOrder", "cancelOrder")) for x in fake.calls), str([x[0] for x in fake.calls]))
net = {p["tradingsymbol"]: p["quantity"] for p in c.positions()["net"]}
check("positions come from the ledger: hedge long 130, main short −130", net == {hedge: 130, tgt: -130}, str(net))
od = c.orders()
check("order book shows both as COMPLETE with fill prices at the live LTP",
      len(od) == 2 and all(o["status"] == "COMPLETE" and o["average_price"] > 0 and o["order_id"].startswith("PAPER") for o in od), str(od))
c2 = AngelClient("APIKEY", "JWT-ABC", home=home, session=fake, instruments=c.instruments_table, paper=True, public_ip="1.1.1.1", min_interval=0, sleep=lambda s: None)
check("the ledger survives a restart", {p["tradingsymbol"]: p["quantity"] for p in c2.positions()["net"]} == net)
om.place_buy_order(c, tgt, 130, price=opt_price(BY_SYMBOL[tgt], SPOT) - 40)       # far below the market: rests
resting = [o for o in c.orders() if o["status"] == "OPEN"]
check("a non-marketable limit order rests as OPEN", len(resting) == 1 and resting[0]["pending_quantity"] == 130)
fake.spot = SPOT - 300                                                              # the market falls through the limit
net2 = {p["tradingsymbol"]: p["quantity"] for p in c.positions()["net"]}
check("when the market reaches it, it fills (position closes) the next time the books are read", net2.get(tgt, 0) == 0, str(net2))
c3, f3, _ = make_client(paper=True)
rr = om.place_buy_order(c3, tgt, 65, price=1.0)
c3.modify_order(order_id=rr.order_id, price=1.5)
c3.cancel_order(order_id=rr.order_id)
check("modify then cancel on a resting paper order", next(o for o in c3.orders() if o["order_id"] == rr.order_id)["status"] == "CANCELLED")
check("cancelling a filled paper order is refused", raises(ValueError, c.cancel_order, order_id=[o for o in c.orders() if o["status"] == "COMPLETE"][0]["order_id"]) is not None)
old = PaperLedger(home / "paper_ledger.json", today=TODAY + timedelta(days=1))
check("a new day starts with an empty ledger (the bot is intraday-only)", old.orders == [])

print("\n[9] the bot itself, running on the adapter")
import main                                           # noqa: E402
from signals.trade_monitor import TradeState         # noqa: E402
from data import option_chain as ocmod              # noqa: E402
ocmod._kite_instruments_cache.clear(); ocmod._oi_snapshot.clear()
c, fake, _ = make_client(paper=True)
t0 = len(fake.calls)
chain = ocmod._fetch_option_chain_from_kite(c)
check("the bot's own option-chain builder works on Angel data (spot, PCR, walls, ~100 quotes in a few requests)",
      chain is not None and chain.spot_price == SPOT and chain.pcr > 0 and chain.call_wall and chain.put_wall and len(chain.call_data) > 40 and (len(fake.calls) - t0) < 10,
      f"{chain} calls={len(fake.calls) - t0}")
check("…and picked the nearest expiry", chain is not None and chain.weekly_expiry_date == EXP1.strftime("%d-%b-%Y"), str(getattr(chain, "weekly_expiry_date", None)))
main.monitor.clear_trade(); main.strangle_legs = None
r = om.place_spread_entry(c, tgt, hedge, 130, sell_price=price, hedge_price=opt_price(BY_SYMBOL[hedge], SPOT))
tr = TradeState(action="CALL_SELL", strike=22800, symbol=tgt, entry_time=datetime.now() - timedelta(minutes=5), entry_premium=price, entry_spot=SPOT,
                sl_spot_level=22900.0, expiry=EXP1.strftime("%d-%b-%Y"), lots=130, hedge_symbol=hedge, hedge_strike=23700, hedge_entry_premium=5.0)
main.monitor.set_trade(tr)
check("main._reconcile_open_trade sees the paper position as open → OK", main._reconcile_open_trade(c) == "OK")
om.place_buy_order(c, tgt, 130, price=price + 5)                                    # the bot (or the user) closes the short
main._ext_close_strikes.clear()
check("after the short is bought back the reconcile reports FLAT (so the bot never double-exits)", main._reconcile_open_trade(c) == "FLAT")
main.monitor.clear_trade()

print("\n[10] login")
from auth import angel_login as AL                    # noqa: E402
home = Path(tempfile.mkdtemp()); tf = home / ".access_token"
fk = FakeAngel()
cl = AngelClient("APIKEY", None, home=home, session=fk, paper=False, public_ip="1.1.1.1", min_interval=0, sleep=lambda s: None)
import pyotp                                          # noqa: E402
secret = pyotp.random_base32()
jwt = AL.login("APIKEY", "A123456", "1234", secret, client=cl, token_file=tf)
body = [x for x in fk.calls if x[0].endswith("loginByPassword")][0]
check("logs in with client code + PIN + a 6-digit TOTP, no Authorization header",
      jwt == "JWT-ABC" and body[1]["clientcode"] == "A123456" and re.fullmatch(r"\d{6}", body[1]["totp"]) and "Authorization" not in body[3], str(body[1]))
check("token file saved with today's date and owner-only permissions", json.loads(tf.read_text())["date"] == str(date.today()) and stat.S_IMODE(tf.stat().st_mode) == 0o600)
check("today's token loads", AL.load_access_token(tf) == "JWT-ABC")
tf.write_text(json.dumps({"access_token": "OLD", "date": "2020-01-01"}))
check("yesterday's token is rejected", raises(ValueError, AL.load_access_token, tf) is not None)
check("no token file → clear error", raises(FileNotFoundError, AL.load_access_token, home / "nope") is not None)
check("a wrong PIN raises Angel's message", "Invalid credentials" in str(raises(AngelError, AL.login, "APIKEY", "A1", "WRONG", secret, client=cl, token_file=tf)))

print("\n[11] broker selection, profile, onboarding")
import config, broker, userprofile                    # noqa: E402
orig = (config.BROKER, config.BROKER_PAPER)
config.BROKER = "angel"; config.BROKER_PAPER = True
os.environ["ANGEL_API_KEY"] = "KEY"
cc = broker.make_client("TOK")
check("factory returns the Angel client in paper mode when the profile says so", isinstance(cc, AngelClient) and cc.paper is True and cc.api_key == "KEY")
config.BROKER = "zerodha"
check("…and Zerodha's client otherwise (unchanged path)", type(broker.make_client("TOK")).__name__ == "KiteConnect")
config.BROKER, config.BROKER_PAPER = orig
os.environ.pop("ANGEL_API_KEY", None)
GOOD = '[broker]\nname = "angel"\n[capital]\ntotal = 1000000\n[user]\nname = "Raj"\n'
st = userprofile.build(userprofile.parse(GOOD), vars(config), GOOD)
check("an Angel profile builds and DEFAULTS TO PAPER MODE", st.broker == "angel" and st.paper is True and st.config_values["BROKER"] == "angel" and st.config_values["BROKER_PAPER"] is True)
live = GOOD.replace('name = "angel"', 'name = "angel"\npaper = false')
check("paper = false switches real orders on", userprofile.build(userprofile.parse(live), vars(config), live).paper is False)
zp = '[broker]\nname = "zerodha"\npaper = true\n[capital]\ntotal = 1000000\n'
e = raises(userprofile.ProfileError, userprofile.build, userprofile.parse(zp), vars(config), zp)
check("paper = true with Zerodha is refused (it would silently place real orders)", e is not None and "only available with Angel" in str(e), str(e))
zd = userprofile.build(userprofile.parse('[broker]\nname = "zerodha"\n[capital]\ntotal = 1000000\n'), vars(config))
check("a Zerodha profile is never paper", zd.paper is False and zd.config_values["BROKER_PAPER"] is False)
check("Angel needs its four secrets", userprofile.missing_secrets("angel", {}) == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"])
from notifications import onboarding                  # noqa: E402
m = onboarding.welcome_messages(st)
check("the welcome guide says PAPER MODE loudly for Angel, and stays valid Markdown", "PAPER MODE" in m[0] and "PAPER MODE" in m[2] and all(md_ok(x) for x in m) and all(len(x) < 3800 for x in m))
check("…and does not mention paper mode for Zerodha", "PAPER" not in " ".join(onboarding.welcome_messages(zd)))
sent = []
paths_mod = __import__("paths"); paths_mod.BROKER_OK_FLAG = Path(tempfile.mkdtemp()) / "flag"
onboarding.send_broker_verified_once(st, post=lambda t: sent.append(t) or True)
check("the 'broker connected' line says orders are simulated", "SIMULATED" in sent[0] and md_ok(sent[0]), str(sent))
tpl = userprofile.render_template("Raj", "angel", 1_000_000)
check("the Angel profile template carries paper = true by default", "paper = true" in tpl and userprofile.parse(tpl)["broker"]["paper"] is True)
check("rendering with paper=False writes paper = false", "paper = false" in userprofile.render_template("Raj", "angel", 1_000_000, paper=False))

print("\n[12] provisioning an Angel user")
spec = importlib.util.spec_from_file_location("provision_user", ROOT / "scripts" / "provision_user.py")
prov = importlib.util.module_from_spec(spec); spec.loader.exec_module(prov)
base = Path(tempfile.mkdtemp())
rc = prov.main(["raj", "--capital", "1000000", "--broker", "angel", "--base", str(base), "--no-seed"])
envtxt = (base / "raj" / ".env").read_text()
check("--broker angel writes the Angel secrets template, not Zerodha's", rc == 0 and "ANGEL_API_KEY=" in envtxt and "ANGEL_TOTP_SECRET=" in envtxt and "KITE_API_KEY" not in envtxt)
check("the generated profile is paper mode", 'name = "angel"' in (base / "raj" / "profile.toml").read_text() and "paper = true" in (base / "raj" / "profile.toml").read_text())
rc = prov.main(["sam", "--capital", "1000000", "--broker", "angel", "--live", "--base", str(base), "--no-seed"])
check("--live writes paper = false", rc == 0 and "paper = false" in (base / "sam" / "profile.toml").read_text())
rc = prov.main(["zed", "--capital", "1000000", "--base", str(base), "--no-seed"])
check("Zerodha provisioning is unchanged (Kite secrets, no paper line)", rc == 0 and "KITE_API_KEY=" in (base / "zed" / ".env").read_text() and "paper" not in (base / "zed" / "profile.toml").read_text())
probe = ("import json, config;print(json.dumps(dict(b=config.BROKER, p=config.BROKER_PAPER, key=config.os.environ.get('ANGEL_API_KEY',''), "
         "limit=config.DAILY_MAX_LOSS)))")
(base / "raj" / ".env").write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n")                     # omits the Angel key
r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env={**os.environ, "ALGO_USER_DIR": str(base / "raj"), "ANGEL_API_KEY": "OWNERS-ANGEL-KEY"}, capture_output=True, text=True)
out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
check("config loads the profile's broker and paper flag; an inherited Angel key never leaks in", out.get("b") == "angel" and out.get("p") is True and out.get("key") == "", r.stderr[-300:] + str(out))
r = subprocess.run([sys.executable, "-c", "import config;print(config.BROKER, config.BROKER_PAPER)"], cwd=ROOT, env={k: v for k, v in os.environ.items() if k != "ALGO_USER_DIR"}, capture_output=True, text=True)
check("the owner's instance is still Zerodha, live", r.stdout.split()[-2:] == ["zerodha", "False"], r.stdout + r.stderr[-200:])

print("\n[13] the verification script (what you run against a real account)")
vspec = importlib.util.spec_from_file_location("verify_angel", ROOT / "scripts" / "verify_angel.py")
V = importlib.util.module_from_spec(vspec); vspec.loader.exec_module(V)
open_now = datetime(2026, 10, 28, 11, 0)
out = []
c, fake, _ = make_client()
res = V.run(c, say=out.append, now=open_now)
bad = [r for r in res if r[0] == V.BAD]
check("a healthy fake account passes every check (no ❌)", not bad, str(bad))
joined = "\n".join(out)
check("it reports funds, instruments, price, candles, batching, OI, positions, order book and the bot's own option chain",
      all(w in joined for w in ("funds", "instruments", "Nifty 50 price", "15-minute candles", "quote batch of 50", "option open interest", "positions", "order book", "option chain (bot's own builder)")), joined[-600:])
check("exit code 0 when clean, and it says real orders are still off", V.summarize(res, say=lambda *_: None) == 0)
c, fake, _ = make_client(FakeAngel(oi=False)); out = []
res = V.run(c, say=out.append, now=open_now)
check("zero open interest during market hours is a ❌ (the bot's signals cannot work)", any(r[0] == V.BAD and "open interest" in r[1] for r in res))
check("exit code 1 when something failed", V.summarize(res, say=lambda *_: None) == 1)
c, fake, _ = make_client(FakeAngel(oi=False)); out = []
res = V.run(c, say=out.append, now=datetime(2026, 10, 25, 11, 0))                           # a Sunday
check("outside market hours it warns instead of failing on OI", any("market is closed" in r[1] for r in res) and not any(r[0] == V.BAD and "open interest" in r[1] for r in res))
c, fake, _ = make_client(FakeAngel(max_batch=10)); out = []
res = V.run(c, say=out.append, now=open_now)
check("an account capped at 10 tokens/request is reported with the batch size that worked", any("quote batch of 25" in r[1] and r[0] == V.WARN for r in res) and any("quote batch of 5" in r[1] and r[0] == V.OK for r in res))
c, fake, _ = make_client(FakeAngel()); out = []
res = V.run(c, test_order=True, confirm=lambda p: "no", say=out.append, now=open_now)
check("--test-order places nothing unless you type YES", not any(x[0].endswith("placeOrder") for x in fake.calls))
c, fake, _ = make_client(FakeAngel()); out = []
res = V.run(c, test_order=True, confirm=lambda p: "YES", say=out.append, now=open_now)
labels = [r[1] for r in res]
check("--test-order lifecycle: placed → in the book → modified → cancelled, all confirmed",
      all(any(w in l for l in labels) for w in ("order placed", "order appears in the order book", "modify price", "cancel")) and not [r for r in res if r[0] == V.BAD], str(res[-5:]))
o = [x[1] for x in fake.calls if x[0].endswith("placeOrder")][0]
check("the test order is 1 lot, a far out-of-the-money PUT, BUY LIMIT ₹0.05", o["quantity"] == "65" and o["tradingsymbol"].endswith("PE") and o["transactiontype"] == "BUY" and float(o["price"]) == 0.05 and float(BY_SYMBOL[o["tradingsymbol"]]["strike"]) / 100 <= SPOT - 1500, str(o))

print("\n[14] a FULL bot scan on the adapter (paper mode): candles → signals → option chain → entry → order → Telegram")
import random                                          # noqa: E402
import pandas as pd                                    # noqa: E402
random.seed(4)
walk, px = [], SPOT - 150
base = datetime(2026, 10, 19, 9, 15)
for i in range(150):                                  # 150 synthetic 15-min bars of a drifting market
    o = px; px += random.uniform(-14, 15); h = max(o, px) + random.uniform(0, 8); l = min(o, px) - random.uniform(0, 8)
    day, slot = divmod(i, 25)
    walk.append([(base + timedelta(days=day, minutes=15 * slot)).strftime("%Y-%m-%dT%H:%M:%S+0530"), o, h, l, px, 0])
import data.sr_database as _srdb                         # noqa: E402
_srdb._DB_FILE = Path(tempfile.mkdtemp()) / "sr_levels.json"   # the scan WRITES S/R levels from its (synthetic) candles — never let that touch the real database
fk = FakeAngel()
orig_request = fk.request
def request(method, url, headers=None, timeout=None, data=None):
    if url.endswith("getCandleData"):
        fk.calls.append((url.replace(A.ROOT_URL, ""), json.loads(data), method, headers or {}))
        return Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": walk})
    return orig_request(method, url, headers=headers, timeout=timeout, data=data)
fk.request = request
fk.spot = walk[-1][4]
c, fake, home = make_client(fk, paper=True)
ocmod._kite_instruments_cache.clear(); ocmod._oi_snapshot.clear()
sent, errs = [], []
saved = (main._post, main.send_error_alert, main._get_kite, main._is_market_open, main.send_signal, ocmod._fetch_with_retry)
main._post = lambda t: sent.append(t) or True
main.send_error_alert = lambda t: errs.append(t)
main.send_signal = lambda *a, **k: sent.append("signal") or True
main._get_kite = lambda: c
main._is_market_open = lambda: True
ocmod._fetch_with_retry = lambda: (_ for _ in ()).throw(RuntimeError("NSE blocked"))       # force the broker-built chain, as in production
main.monitor.clear_trade(); main.strangle_legs = None
logs = []
hdl = logging.Handler(); hdl.emit = lambda r: logs.append((r.levelname, r.getMessage())); logging.getLogger().addHandler(hdl)
try:
    main.run_scan()
finally:
    logging.getLogger().removeHandler(hdl)
    main._post, main.send_error_alert, main._get_kite, main._is_market_open, main.send_signal, ocmod._fetch_with_retry = saved
bad = [m for lv, m in logs if lv in ("ERROR", "CRITICAL") and "NSE" not in m and "Option chain fetch failed" not in m]
check("a whole scan runs on Angel data with no scan error and no alert", not bad and not errs, str(bad[:3]) + str(errs[:2]))
check("the scan really used the adapter's data (a signal line was produced from the broker-built chain)", any("Signal:" in m or "Option signal:" in m for _, m in logs), str([m for _, m in logs][-4:]))
real_orders = [x for x in fk.calls if x[0].endswith(("placeOrder", "modifyOrder", "cancelOrder"))]
check("paper mode: even if the scan entered a trade, NO order reached the (fake) Angel API", real_orders == [], str(real_orders))
main.monitor.clear_trade()

print("   forced signal → the whole entry path inside run_scan()")
from signals.combiner import FinalSignal               # noqa: E402
atm_k = round(fk.spot / 50) * 50
sell_k, hedge_k = atm_k + 100, atm_k + 700
sell_s, hedge_s = sym(EXP1, sell_k, "CE"), sym(EXP1, hedge_k, "CE")
sell_px, hedge_px = opt_price(BY_SYMBOL[sell_s], fk.spot), opt_price(BY_SYMBOL[hedge_s], fk.spot)
forced = FinalSignal(action="CALL_SELL", strength="STRONG", score=3, lots=2, strike=sell_k, symbol=sell_s, premium=sell_px,
                     sl_spot_level=fk.spot + 60, hedge_call_strike=hedge_k, hedge_call_symbol=hedge_s, hedge_call_ltp=hedge_px,
                     expiry=EXP1.strftime("%d-%b-%Y"), spot_price=fk.spot)
saved2 = (main.combine_signals, main._entry_allowed, main._STATE_FILE, main._post, main.send_error_alert, main.send_signal, main._get_kite, main._is_market_open, ocmod._fetch_with_retry)
main.combine_signals = lambda **k: forced
main._entry_allowed = lambda: True
main._STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
sent.clear(); errs.clear(); fk.calls.clear()
main._post = lambda t: sent.append(t) or True
main.send_error_alert = lambda t: errs.append(t)
main.send_signal = lambda *a, **k: sent.append("signal") or True
main._get_kite = lambda: c
main._is_market_open = lambda: True
ocmod._fetch_with_retry = lambda: (_ for _ in ()).throw(RuntimeError("NSE blocked"))
main.monitor.clear_trade(); main.strangle_legs = None
try:
    main.run_scan()
finally:
    (main.combine_signals, main._entry_allowed, main._STATE_FILE, main._post, main.send_error_alert, main.send_signal,
     main._get_kite, main._is_market_open, ocmod._fetch_with_retry) = saved2
book = {p["tradingsymbol"]: p["quantity"] for p in c.positions()["net"]}
check("the bot entered: it tracks a CALL_SELL on the right strike and the state file was written", main.monitor.trade is not None and main.monitor.trade.strike == sell_k and main.monitor.trade.symbol == sell_s, str(main.monitor.trade))
q = main.monitor.trade.lots if main.monitor.trade else -1                            # whatever size the bot decided (the 14:00 late-entry cut may halve it, depending on the clock)
check("hedge BOUGHT and short leg SOLD in the paper books, same quantity the bot believes it holds",
      q > 0 and q == forced.lots * 65 and book.get(hedge_s) == q and book.get(sell_s) == -q, str((q, book)))
check("no real order reached Angel, no error alert", not [x for x in fk.calls if x[0].endswith(("placeOrder", "modifyOrder", "cancelOrder"))] and not errs, str((errs, [x[0] for x in fk.calls][-3:])))
check("and the bot's own safety check agrees the position exists (reconcile → OK)", main._reconcile_open_trade(c) == "OK")
main.monitor.clear_trade()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
