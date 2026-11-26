"""
Offline tests for the subscriber executor (executor/). No broker, no Telegram, no real orders: brokers are
faked, the clock is simulated, and instructions come from the REAL pipeline (signal feed → executor_feed →
executor_api.instructions_for), plus one HTTPS round trip with the pinned certificate.
Run from the repo root:   python3 tests/test_executor.py
"""
import hashlib, json, os, socket, ssl, subprocess, sys, tempfile, threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, executor_feed as EF, executor_api as EA, x_poster as X
from executor import config as C, venue as V
from executor.engine import Engine, IST
from executor.api import Api, ApiError
from signals.trade_monitor import TradeState

TMP = Path(tempfile.mkdtemp())
LOT = 65
CLOCK = [datetime(2026, 11, 27, 9, 0, tzinfo=IST)]
feed._now = lambda: CLOCK[0]
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def at(hh, mm, ss=0, d=27):
    CLOCK[0] = datetime(2026, 11, d, hh, mm, ss, tzinfo=IST)


# ── fakes ─────────────────────────────────────────────────────────────────────
class ScriptVenue:
    """Records every call; fills `fill` share of each order (1.0 = full) unless told otherwise per symbol."""
    stops_supported = True

    def __init__(self):
        self.calls, self.fill, self.fill_by_symbol, self.spot_px, self.n = [], 1.0, {}, None, 0
        self.stops = {}

    def execute(self, side, symbol, qty, ref):
        frac = self.fill_by_symbol.pop(symbol, self.fill) if symbol in self.fill_by_symbol else self.fill
        got = int(qty * frac) // LOT * LOT
        self.calls.append(("EXEC", side, symbol, qty, got))
        return V.Fill(got, ref or 100.0)

    def place_stop(self, symbol, qty, trigger):
        self.n += 1
        oid = f"SL{self.n}"
        self.stops[oid] = (symbol, qty, round(trigger, 2))
        self.calls.append(("STOP", symbol, qty, round(trigger, 2)))
        return oid

    def cancel_stop(self, oid):
        if oid:
            self.stops.pop(oid, None)
            self.calls.append(("CANCEL", oid))

    def spot(self):
        return self.spot_px


class LocalApi:
    """The real executor_api.instructions_for, without HTTP."""
    def __init__(self, sub, owner=False):
        self.sub, self.owner = sub, owner

    def status(self):
        latest = EF.read_since(0)
        return {"plan": self.sub["plan"], "status": "paid", "latest_seq": latest[-1]["seq"] if latest else 0}

    def instructions(self, since):
        return EA.instructions_for(self.sub, self.owner, since, CLOCK[0])


def settings(**kw):
    s = C.Settings(home=TMP, api_key="tdx_x", api_pin="0" * 64)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def pipeline(tag):
    feed.OUTBOX = TMP / f"{tag}_outbox.jsonl"
    feed.STATE = TMP / f"{tag}_feed.json"
    EF.INSTRUCTIONS, EF.STATE = TMP / f"{tag}_ins.jsonl", TMP / f"{tag}_exec.json"
    feed._mark = None
    EF.update(feed.OUTBOX)


def sync():
    EF.update(feed.OUTBOX)


def engine(tag, plan="small", **kw):
    v = ScriptVenue()
    sub = {"plan": plan, "paid_until": "2030-01-01T00:00:00+05:30"}
    e = Engine(settings(**kw), v, LocalApi(sub), state_path=TMP / f"{tag}_state.json", clock=lambda: CLOCK[0])
    e.start()
    return e, v


def call_sell(lots, strike=26000, prem=180.0, hedge=27050, hprem=5.0, sl=26050):
    return TradeState(action="CALL_SELL", strike=strike, symbol=f"NIFTY26D01{strike}CE", entry_time=CLOCK[0],
                      entry_premium=prem, entry_spot=25945, sl_spot_level=sl, expiry="01-Dec-2026",
                      lots=lots * LOT, hedge_symbol=f"NIFTY26D01{hedge}CE", hedge_strike=hedge, hedge_entry_premium=hprem)


# ── 1. the broker venue: limit orders that cross, confirmed fills, re-pricing ──
print("broker venue")
check("limit prices cross the market by max(₹2, 1%), on the 0.05 tick",
      V.cross(100.0, "BUY") == 102.0 and V.cross(100.0, "SELL") == 98.0 and V.cross(300.0, "BUY") == 303.0
      and V.cross(1.0, "SELL") == 0.05 and V.cross(100.0, "BUY", 1) == 104.0)


class FakeKite:
    def __init__(self, behaviour):
        self.b, self.placed, self.modified, self.cancelled, self.book = behaviour, [], [], [], {}
        self.t = [0.0]

    def ltp(self, keys):
        return {k: {"last_price": 100.0} for k in keys}

    def place_order(self, **kw):
        oid = str(len(self.placed) + 1)
        self.placed.append(kw)
        st = {"fill": ("COMPLETE", kw["quantity"]), "reject": ("REJECTED", 0), "stuck": ("OPEN", 0),
              "partial": ("OPEN", 65)}[self.b]
        self.book[oid] = {"order_id": oid, "status": st[0], "filled_quantity": st[1], "average_price": kw.get("price"),
                          "status_message": "RMS: margin" if self.b == "reject" else None}
        return oid

    def orders(self):
        return list(self.book.values())

    def modify_order(self, **kw):
        self.modified.append(kw["price"])

    def cancel_order(self, **kw):
        self.cancelled.append(kw["order_id"])
        self.book[kw["order_id"]]["status"] = "CANCELLED"


def venue(b):
    k = FakeKite(b)
    t = [0.0]
    return k, V.BrokerVenue(k, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])


k, bv = venue("fill")
f = bv.execute("SELL", "NIFTY26D0126000CE", 130, 100.0)
check("fill: LIMIT order, NRML, confirmed from the order book", f.qty == 130 and k.placed[0]["order_type"] == "LIMIT"
      and k.placed[0]["price"] == 98.0 and k.placed[0]["product"] == "NRML", (f, k.placed))
k, bv = venue("stuck")
f = bv.execute("BUY", "X", 65, 100.0)
check("no fill: re-priced 3 times (wider each time), then cancelled", f.qty == 0 and k.modified == [104.0, 106.0, 108.0]
      and k.cancelled == ["1"] and "cancelled" in f.note, (f, k.modified))
k, bv = venue("partial")
f = bv.execute("BUY", "X", 130, 100.0)
check("partial: reports what really filled", f.qty == 65, f)
k, bv = venue("reject")
f = bv.execute("SELL", "X", 65, 100.0)
check("rejected: zero fill with the broker's reason", f.qty == 0 and "RMS: margin" in f.note, f)
k, bv = venue("fill")
oid = bv.place_stop("X", 65, 288.0)
check("broker stop = SL order, BUY, trigger on the tick", k.placed[0]["order_type"] == "SL" and k.placed[0]["transaction_type"] == "BUY"
      and k.placed[0]["trigger_price"] == 288.0 and k.placed[0]["price"] == 302.4, k.placed)

# ── 2. Rahul's real 26 Nov day, end to end ────────────────────────────────────
print("real day, small plan (1 lot)")
pipeline("real")
at(9, 0); e, v = engine("real_small", "small")
at(10, 30); t1 = call_sell(2, prem=167.80, hedge=27100, hprem=5.30, sl=26020)
feed.trade_entry(t1, 25917, strength="MODERATE"); sync(); e.tick()
check("open: BUY hedge, then SELL main, then a broker stop at entry +60%",
      v.calls[:3] == [("EXEC", "BUY", "NIFTY26D0127100CE", 65, 65), ("EXEC", "SELL", "NIFTY26D0126000CE", 65, 65),
                      ("STOP", "NIFTY26D0126000CE", 65, round(167.80 * 1.6, 2))], v.calls[:3])
at(12, 0); feed.trade_exit(t1, 172.80, -812.0, "PROFIT_LOCK", 25935); sync(); v.calls.clear(); e.tick()
check("close: stop CANCELLED before the buy-back, then hedge sold, then flat",
      [c[0] for c in v.calls] == ["CANCEL", "EXEC", "EXEC"] and v.calls[1][1:3] == ("BUY", "NIFTY26D0126000CE")
      and v.calls[2][1:3] == ("SELL", "NIFTY26D0127100CE") and e.st["positions"] == {}, v.calls)
at(12, 15); t2 = call_sell(2); feed.trade_entry(t2, 25945, strength="STRONG"); sync(); e.tick()
at(14, 0); feed.trade_exit(replace(t2, lots=LOT), 96.20, 5346.0, "PROFIT_BOOK", 25780); sync(); v.calls.clear(); e.tick()
check("owner books 1 of 2: the 1-lot follower holds (no orders)", v.calls == [] and len(e.st["positions"]) == 1)
at(14, 45); t2.lots = LOT; feed.trade_exit(t2, 151.25, 1732.0, "PROFIT_LOCK", 25908); sync(); v.calls.clear(); e.tick()
check("…and exits its lot at the final close", [c[:3] for c in v.calls if c[0] == "EXEC"] == [("EXEC", "BUY", "NIFTY26D0126000CE"),
      ("EXEC", "SELL", "NIFTY26D0127050CE")] and e.st["positions"] == {})

print("real day, standard plan (3/2/1)")
pipeline("real3")
at(9, 0); e, v = engine("real_std", "standard")
at(12, 15); t2 = call_sell(2); feed.trade_entry(t2, 25945, strength="STRONG"); sync(); e.tick()
check("strong: 3 lots + stop for 195", ("EXEC", "SELL", "NIFTY26D0126000CE", 195, 195) in v.calls
      and ("STOP", "NIFTY26D0126000CE", 195, 288.0) in v.calls, v.calls)
at(14, 0); feed.trade_exit(replace(t2, lots=LOT), 96.20, 5346.0, "PROFIT_BOOK", 25780); sync(); v.calls.clear(); e.tick()
check("booking: cancel stop, buy back 2 lots, sell 2 hedge, new stop for the 1 lot left",
      [c[0] for c in v.calls] == ["CANCEL", "EXEC", "STOP", "EXEC"] and v.calls[1][3] == 130 and v.calls[2][2] == 65
      and v.calls[3][1:4] == ("SELL", "NIFTY26D0127050CE", 130), v.calls)

# ── 3. the safety rules ───────────────────────────────────────────────────────
print("safety rules")
pipeline("stale")
at(9, 0); e, v = engine("stale")
at(10, 0); t = call_sell(2); feed.trade_entry(t, 25900, strength="STRONG"); sync()
at(10, 3); e.tick()
check("rule 1: entry 3 min old → skipped, nothing traded", v.calls == [] and e.st["positions"] == {}
      and any("stale entry" in x for x in e.events), e.events)
at(10, 30); feed.trade_exit(t, 90.0, 5850.0, "TARGET", 25800); sync(); e.tick()
check("…and its later exit does nothing (not held here)", v.calls == [])

pipeline("late")
at(9, 0); e, v = engine("late")
at(14, 56); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
check("rule 2: no new position after 14:55", e.st["positions"] == {} and any("outside" in x for x in e.events))

pipeline("short")
at(9, 0); e, v = engine("short", "standard")
v.fill_by_symbol["NIFTY26D0126000CE"] = 1 / 3                     # hedge 3 lots fill, main only 1
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
pos = next(iter(e.st["positions"].values()))
check("rule 3: main SELL fills 1 of 3 → the 2 extra hedge lots are sold straight back",
      pos["legs"]["CE"]["qty"] == 65 and pos["hedges"]["CE"]["qty"] == 65
      and ("EXEC", "SELL", "NIFTY26D0127050CE", 130, 130) in v.calls, (pos, v.calls))

pipeline("nohedge")
at(9, 0); e, v = engine("nohedge")
v.fill_by_symbol["NIFTY26D0127050CE"] = 0
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
check("rule 3: hedge BUY fails → main is NOT sold (never naked)", e.st["positions"] == {}
      and not any(c[:2] == ("EXEC", "SELL") and c[4] for c in v.calls), v.calls)

pipeline("cap")
at(9, 0); e, v = engine("cap", "standard", max_lots_per_leg=2)
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
check("rule 4: the per-leg cap cuts a 3-lot sell to 2, and the spare hedge is unwound",
      next(iter(e.st["positions"].values()))["legs"]["CE"]["qty"] == 130 and any("cap" in x for x in e.events))

pipeline("flat")
at(9, 0); e, v = engine("flat")
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
at(15, 24); v.calls.clear(); e.tick()
check("before 15:25: position kept", e.st["positions"] and v.calls == [])
at(15, 25); e.tick()
check("rule 2: 15:25 flatten closes everything on its own", e.st["positions"] == {}
      and [c[:2] for c in v.calls if c[0] == "EXEC"] == [("EXEC", "BUY"), ("EXEC", "SELL")], v.calls)

pipeline("guard")
at(9, 0); e, v = engine("guard")
at(10, 0); feed.trade_entry(call_sell(2, sl=26050), 25900, strength="STRONG"); sync(); e.tick()
v.spot_px = 26060
at(10, 5); e.tick()
check("rule 6: spot past the stop → waits for the bot's exit instruction", e.st["positions"] and any("waiting" in x for x in e.events))
at(10, 6); e.tick()
check("…still waiting after 60s", bool(e.st["positions"]))
v.spot_px = 26040
at(10, 6, 30); e.tick()
check("…back inside: the clock resets", next(iter(e.st["positions"].values()))["breach_since"] is None)
v.spot_px = 26070
at(10, 8); e.tick(); at(10, 10, 1); e.tick()
check("…past the stop for 2 min with no exit → the executor exits itself", e.st["positions"] == {}
      and any("local stop" in x for x in e.events), e.events[-3:])

pipeline("retry")
at(9, 0); e, v = engine("retry")
at(10, 0); t = call_sell(2); feed.trade_entry(t, 25900, strength="STRONG"); sync(); e.tick()
v.fill = 0                                                        # the broker fills nothing for a whole pass
at(11, 0); feed.trade_exit(t, 90.0, 5850.0, "TARGET", 25800); sync(); e.tick()
v.fill = 1.0
pos = next(iter(e.st["positions"].values()))
check("rule 5+7: failed buy-back keeps the hedge, re-places the stop, and is retried",
      pos["legs"]["CE"]["qty"] == 65 and pos["hedges"]["CE"]["qty"] == 65 and pos["stops"].get("CE")
      and pos["target"] == {"CE": 0}, pos)
at(11, 0, 2); e.tick()
check("…next loop: closed and flat", e.st["positions"] == {} and not v.stops, (e.st["positions"], v.stops))

pipeline("restart")
at(9, 0); e, v = engine("restart")
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync(); e.tick()
e2 = Engine(settings(), v, e.api, state_path=TMP / "restart_state.json", clock=lambda: CLOCK[0])
check("restart: open position, stop and cursor survive", e2.st["positions"] and e2.st["cursor"] == 1
      and next(iter(e2.st["positions"].values()))["stops"])
pipeline("fresh")
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync()
at(10, 0, 30); e, v = engine("fresh")
e.tick()
check("first start mid-trade: follows from now, never joins the open trade late", v.calls == [] and e.st["cursor"] == 1)

# ── 4. over real HTTPS, with the pinned certificate ───────────────────────────
print("HTTPS client")
tls = TMP / "tls"; tls.mkdir()
subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-keyout", str(tls / "key.pem"), "-out", str(tls / "cert.pem"), "-days", "2", "-subj", "/CN=t",
                "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert((tls / "cert.pem").read_text())).hexdigest()
pipeline("https")
key, h = EA.new_key()
EA.SUBS = TMP / "https_subs.json"
EA.SUBS.write_text(json.dumps({"501": {"plan": "small", "paid_until": "2030-01-01T00:00:00+05:30", "exec_key_sha256": h,
                                        "exec_key_created": "2026-01-01T00:00:00+05:30"}}))
EA.TG_ENV = TMP / "none.env"
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync()
s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
httpd = EA.serve(port=port, tls_dir=tls, host="127.0.0.1")
threading.Thread(target=httpd.serve_forever, daemon=True).start()
api = Api(f"https://127.0.0.1:{port}", pin, key)
check("status over HTTPS", api.status()["plan"] == "small")
r = api.instructions(0)
check("instructions over HTTPS: the OPEN at 1 lot", r["instructions"][0]["kind"] == "OPEN"
      and [o["qty"] for o in r["instructions"][0]["orders"]] == [65, 65], r)
try:
    Api(f"https://127.0.0.1:{port}", "f" * 64, key).status(); refused = False
except ApiError as ex:
    refused = "does not match the pinned fingerprint" in str(ex)
check("a different server certificate is refused", refused)
try:
    Api(f"https://127.0.0.1:{port}", pin, "tdx_bad").status(); bad = False
except ApiError as ex:
    bad = ex.status == 401
check("a wrong key is refused (401)", bad)
httpd.shutdown()

# ── 5. settings checks ────────────────────────────────────────────────────────
print("settings")
home = TMP / "cfg"; home.mkdir()
(home / "executor.env").write_text("EXEC_API_KEY=tdx_abc\nEXEC_API_PIN=" + "a" * 64 + "\nBROKER=zerodha\nMODE=live\nZERODHA_USER_ID=AB1\n")
s = C.load(home)
p = s.problems()
check("missing broker credentials are listed, live mode recognised", s.live and "ZERODHA_PASSWORD is missing" in p
      and "KITE_API_SECRET is missing" in p and len(p) == 4, p)
(home / "executor.env").write_text("EXEC_API_KEY=tdx_abc\nEXEC_API_PIN=" + "a" * 64 + "\nBROKER=paper\nMODE=live\n")
check("live mode with no broker is refused", "MODE=live needs BROKER=zerodha or angel" in C.load(home).problems())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
