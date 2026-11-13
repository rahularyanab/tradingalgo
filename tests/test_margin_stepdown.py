"""
Offline tests for "refused for margin -> retry at once with one lot fewer, down to 1 lot" (main._enter_with_stepdown and its
three callers: new entry, reversal re-entry, strangle). A fake broker refuses a SELL above a per-symbol lot cap with Zerodha's
own wording ("Insufficient funds. Margin required: ..."); main.run_scan runs for real against it.
python3 tests/test_margin_stepdown.py        — every state file is redirected to a temp dir.
"""
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""     # tests must never post to the live Telegram bot (the local .env holds its token)
os.environ.pop("ALGO_USER_DIR", None)

import paths                                                      # noqa: E402
TMP = Path(tempfile.mkdtemp())
paths.PAUSE_FILE = TMP / ".paused"; paths.GLOBAL_PAUSE_FILE = TMP / "pause_all.flag"
paths.STATE_FILE = TMP / "state.json"; paths.ENTRY_FAILS_FILE = TMP / ".entry_fails"

import config                                                     # noqa: E402
import entry_guard                                                # noqa: E402
import data.sr_database as _srdb                                  # noqa: E402
_srdb._DB_FILE = TMP / "sr.json"
import main                                                       # noqa: E402
import broker.angel as A                                          # noqa: E402
from data import option_chain as ocmod                            # noqa: E402
from signals.combiner import FinalSignal                          # noqa: E402
from fakes_angel import FakeAngel, make_client, sym, opt_price, BY_SYMBOL, EXP1, SPOT, Resp   # noqa: E402

LOT = config.NIFTY_LOT_SIZE
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra and not cond else ""))


MARGIN_MSG = "Insufficient funds. Margin required: 717681.61. Margin available: 713351.84. Add 4329.77 to place this order."

print("[1] the margin-error detector")
for msg in (MARGIN_MSG, "RMS:Margin Exceeds, Required:1,23,000", "Insufficient margin", "not enough margin for this order"):
    check(f"margin wording recognised: {msg[:40]!r}", main._is_margin_error(msg))
for msg in ("Order rejected: price out of range", "Hedge buy failed", "network error calling placeOrder", "", None, "Invalid token"):
    check(f"NOT a margin error: {msg!r}", not main._is_margin_error(msg))

print("\n[2] _enter_with_stepdown on its own")
posts = []
main._post = lambda t: posts.append(t) or True


def stepper(cap_lots, err=MARGIN_MSG, clean=True):
    tried = []

    def attempt(q):
        tried.append(q // LOT)
        return (True, None, True) if q // LOT <= cap_lots else (False, err, clean)
    return attempt, tried


att, tried = stepper(3)
check("fits at once: one attempt, no message", main._enter_with_stepdown(3 * LOT, att, "X") == (True, 3 * LOT, None) and tried == [3] and not posts)
att, tried = stepper(2); posts.clear()
check("3 refused -> 2 fits: tries [3, 2], enters 2 lots", main._enter_with_stepdown(3 * LOT, att, "X")[:2] == (True, 2 * LOT) and tried == [3, 2], str(tried))
check("it tells you, once: 'entered 2 of 3 lot(s)'", len(posts) == 1 and "2 of 3 lot(s)" in posts[0] and posts[0].count("*") % 2 == 0, str(posts))
att, tried = stepper(1); posts.clear()
check("3 -> 2 -> 1: tries [3, 2, 1], enters 1 lot", main._enter_with_stepdown(3 * LOT, att, "X")[:2] == (True, LOT) and tried == [3, 2, 1], str(tried))
att, tried = stepper(0); posts.clear()
ok, q, err = main._enter_with_stepdown(3 * LOT, att, "X")
check("nothing fits: stops after 1 lot with the broker's own error", not ok and tried == [3, 2, 1] and q == LOT and "Insufficient funds" in err and not posts, str((tried, posts)))
att, tried = stepper(0, err="Order rejected: price out of range")
check("a refusal that is NOT about margin is not retried", main._enter_with_stepdown(3 * LOT, att, "X")[0] is False and tried == [3])
att, tried = stepper(0, clean=False)
check("a refusal that left a position behind is not retried (would stack on the orphan)", main._enter_with_stepdown(3 * LOT, att, "X")[0] is False and tried == [3])
att, tried = stepper(0)
check("1 lot refused: one attempt, as before (nothing to reduce)", main._enter_with_stepdown(LOT, att, "X")[0] is False and tried == [1])

print("\n[3] real scans against a fake broker that caps lots")
random.seed(4)
walk, px = [], SPOT - 150
base = datetime(2026, 10, 19, 9, 15)
for i in range(150):
    o = px; px += random.uniform(-14, 15); h = max(o, px) + random.uniform(0, 8); l = min(o, px) - random.uniform(0, 8)
    day, slot = divmod(i, 25)
    walk.append([(base + timedelta(days=day, minutes=15 * slot)).strftime("%Y-%m-%dT%H:%M:%S+0530"), o, h, l, px, 0])

main.send_error_alert = lambda t: alerts.append(t)
main.send_signal = lambda *a, **k: None
main._is_market_open = lambda: True
main._entry_allowed = lambda: True
main._STATE_FILE = TMP / "main_state.json"
alerts = []


class Broker:
    """A FakeAngel whose SELLs are refused (Zerodha wording) above caps[symbol] lots; fail_sells[symbol] refuses every SELL of it."""
    def __init__(self, caps, fail_sells=(), other_error=None):
        self.f = FakeAngel(); self.f.spot = walk[-1][4]
        self.caps, self.fail_sells, self.other_error, self.log, self.placed = caps, set(fail_sells), other_error, [], []     # log = every attempt, placed = accepted only
        orig = self.f.request

        def req(method, url, headers=None, timeout=None, data=None):
            if url.endswith("getCandleData"):
                self.f.calls.append((url.replace(A.ROOT_URL, ""), json.loads(data), method, headers or {}))
                return Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": walk})
            if url.endswith("placeOrder"):
                p = json.loads(data); s, side, n = p["tradingsymbol"], p["transactiontype"], int(p["quantity"])
                self.log.append((s, side, n // LOT))
                if side == "SELL" and s in self.fail_sells:
                    return Resp({"status": False, "message": "gateway timeout", "errorcode": "AB1000", "data": None})
                if side == "SELL" and s in self.caps and n > self.caps[s] * LOT:
                    return Resp({"status": False, "message": self.other_error or MARGIN_MSG, "errorcode": "AB4036", "data": None})
                self.placed.append((s, side, n // LOT))
            return orig(method, url, headers=headers, timeout=timeout, data=data)
        self.f.request = req
        self.client = make_client(self.f, paper=False)[0]

    def net(self, symbol):                         # lots sold minus bought, accepted orders only
        return sum((n if side == "SELL" else -n) for s, side, n in self.placed if s == symbol)


def run(signal, broker):
    main.combine_signals = lambda **kw: signal
    main._get_kite = lambda: broker.client
    saved = ocmod._fetch_with_retry
    ocmod._fetch_with_retry = lambda: (_ for _ in ()).throw(RuntimeError("NSE blocked"))
    ocmod._kite_instruments_cache.clear(); ocmod._oi_snapshot.clear()
    main.monitor.clear_trade(); main.strangle_legs = None
    posts.clear(); alerts.clear()
    entry_guard.reset()
    main.run_scan()
    ocmod._fetch_with_retry = saved


f0 = FakeAngel(); f0.spot = walk[-1][4]
K = round(f0.spot / 50) * 50 + 100
CE = sym(EXP1, K, "CE"); HCE = sym(EXP1, K + 700, "CE"); PE = sym(EXP1, K - 300, "PE"); HPE = sym(EXP1, K - 1000, "PE")


def directional(lots, hedged):
    kw = dict(hedge_call_symbol=HCE, hedge_call_strike=K + 700, hedge_call_ltp=opt_price(BY_SYMBOL[HCE], f0.spot)) if hedged else {}
    return FinalSignal(action="CALL_SELL", strength="STRONG", score=3, lots=lots, strike=K, symbol=CE, premium=opt_price(BY_SYMBOL[CE], f0.spot),
                       sl_spot_level=f0.spot + 60, expiry=EXP1.strftime("%d-%b-%Y"), spot_price=f0.spot, **kw)


config.ENTRY_FAIL_LIMIT = 2
for hedged in (False, True):
    tag = "hedged" if hedged else "naked"
    b = Broker({CE: 2}); run(directional(3, hedged), b)
    sells = [n for s, side, n in b.log if s == CE and side == "SELL"]
    check(f"[{tag}] 3 lots refused, 2 fit: SELL attempts {sells} -> trade is 2 lots", sells == [3, 2] and main.monitor.trade is not None and main.monitor.trade.lots == 2 * LOT, str(b.log))
    check(f"[{tag}] the trade records its real size, and will not pyramid on top of a margin-limited entry", main.monitor.trade.entry_lots == 2 * LOT and main.monitor.trade.pyramid_disabled)
    check(f"[{tag}] net short is exactly 2 lots; no failed-entry count, no alert; one 'Margin short' note", b.net(CE) == 2 and entry_guard.count() == 0 and not alerts and sum("2 of 3 lot(s)" in m for m in posts) == 1, str((b.log, alerts, posts)))
    if hedged:
        check("[hedged] the hedge is always bought back to match: net long hedge == short lots (no orphan hedge)", -b.net(HCE) == 2, str(b.log))
        check("[hedged] the trade carries the hedge", main.monitor.trade.hedge_symbol == HCE)
    b = Broker({CE: 1}); run(directional(3, hedged), b)
    check(f"[{tag}] only 1 lot fits: attempts 3, 2, 1 -> trade is 1 lot", [n for s, side, n in b.log if s == CE and side == "SELL"] == [3, 2, 1] and main.monitor.trade.lots == LOT and b.net(CE) == 1, str(b.log))
    b = Broker({CE: 0}); run(directional(3, hedged), b)
    check(f"[{tag}] nothing fits: stops after the 1-lot try, no trade, nothing left open", main.monitor.trade is None and b.net(CE) == 0 and (not hedged or b.net(HCE) == 0), str(b.log))
    check(f"[{tag}] ...and that is ONE failed entry (not three), one alert", entry_guard.count() == 1 and len(alerts) == 1 and "Insufficient funds" in alerts[0], str((entry_guard.count(), alerts)))
    b = Broker({CE: 5}); run(directional(3, hedged), b)
    check(f"[{tag}] enough margin: one attempt at full size, pyramiding stays as it was", [n for s, side, n in b.log if s == CE and side == "SELL"] == [3] and main.monitor.trade.lots == 3 * LOT and not main.monitor.trade.pyramid_disabled and not posts)
    b = Broker({CE: 0}, other_error="Order rejected: price out of range"); run(directional(3, hedged), b)
    check(f"[{tag}] a non-margin refusal is NOT retried (one attempt)", [n for s, side, n in b.log if s == CE and side == "SELL"] == [3] and main.monitor.trade is None)
    b = Broker({CE: 0}); run(directional(1, hedged), b)
    check(f"[{tag}] a 1-lot signal is attempted once, exactly as before", [n for s, side, n in b.log if s == CE and side == "SELL"] == [1] and entry_guard.count() == 1)

b = Broker({CE: 0}, fail_sells=[HCE]); run(directional(3, True), b)
check("hedged: if the hedge cannot be sold back (orphan!), it does NOT retry — one attempt, then the loud alert",
      [n for s, side, n in b.log if s == CE and side == "SELL"] == [3] and any("UNWIND FAILED" in a for a in alerts) and main.monitor.trade is None, str((b.log, alerts)))

print("\n[4] strangle: both legs, partial fills unwound before the retry")
ce_p, pe_p = opt_price(BY_SYMBOL[CE], f0.spot), opt_price(BY_SYMBOL[PE], f0.spot)
strangle = FinalSignal(action="STRANGLE", strength="MODERATE", score=2, lots=0, strike=K, symbol=CE, premium=ce_p, sl_spot_level=f0.spot + 60,
                       expiry=EXP1.strftime("%d-%b-%Y"), spot_price=f0.spot, call_strike=K, put_strike=K - 300, call_symbol=CE, put_symbol=PE,
                       call_premium=ce_p, put_premium=pe_p, call_sl=f0.spot + 60, put_sl=f0.spot - 60, strangle_lots=3)
main._strangle_entry_allowed = lambda: True
b = Broker({CE: 3, PE: 2}); run(strangle, b)
check("CE fits 3 but PE only 2: attempt 1 puts CE on then PE is refused -> CE is bought back, attempt 2 enters both at 2 lots",
      main.monitor.trade is not None and main.monitor.trade.lots == 2 * LOT and b.net(CE) == 2 and b.net(PE) == 2, str(b.log))
check("strangle: the entered size is recorded, pyramiding off, one note", main.monitor.trade.pyramid_disabled and sum("2 of 3 lot(s)" in m for m in posts) == 1 and entry_guard.count() == 0, str(posts))
b = Broker({CE: 0, PE: 0}); run(strangle, b)
check("strangle, nothing fits: ends flat (no leg left open), one failed entry, one alert", main.monitor.trade is None and b.net(CE) == 0 and b.net(PE) == 0 and entry_guard.count() == 1 and len(alerts) == 1, str((b.log, alerts)))
b = Broker({CE: 5, PE: 5}); run(strangle, b)
check("strangle, enough margin: unchanged — 3 lots, one attempt per leg", main.monitor.trade.lots == 3 * LOT and not main.monitor.trade.pyramid_disabled and len([1 for s, side, n in b.log if side == "SELL"]) == 2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
