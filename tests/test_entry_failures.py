"""
Offline tests for "N failed entries and the day is over" (entry_guard.py + its hooks in main.py + control integration).
python3 tests/test_entry_failures.py        — every state file is redirected to a temp dir.
"""
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""     # tests must never post to the live Telegram bot (the local .env holds its token)
os.environ.pop("ALGO_USER_DIR", None)

import paths                                                      # noqa: E402
TMP = Path(tempfile.mkdtemp())
paths.PAUSE_FILE = TMP / ".paused"; paths.GLOBAL_PAUSE_FILE = TMP / "pause_all.flag"
paths.STATE_FILE = TMP / "state.json"; paths.ENTRY_FAILS_FILE = TMP / ".entry_fails"

import config                                                     # noqa: E402
import control, entry_guard                                       # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def md_ok(text):
    if text.count("`") % 2:
        return False
    plain = re.sub(r"`[^`]*`", "", text)
    return plain.count("_") % 2 == 0 and plain.count("*") % 2 == 0


def fresh():
    for f in (paths.PAUSE_FILE, paths.GLOBAL_PAUSE_FILE, paths.ENTRY_FAILS_FILE):
        f.unlink(missing_ok=True)


print("[1] entry_guard")
fresh(); config.ENTRY_FAIL_LIMIT = 0
check("limit 0 (default): feature is OFF — nothing recorded, never halts", entry_guard.record_failure("x") == (0, False) and not paths.ENTRY_FAILS_FILE.exists() and not entry_guard.halted_today())
check("the shipped default is 0, so the owner's own bot is unchanged", __import__("importlib").reload(config).ENTRY_FAIL_LIMIT == 0)
config.ENTRY_FAIL_LIMIT = 2
check("1st failure: counted, not halted", entry_guard.record_failure("margin") == (1, False) and not entry_guard.halted_today())
check("2nd failure: halted", entry_guard.record_failure("margin again") == (2, True) and entry_guard.halted_today())
check("it is only 'today': tomorrow it has reset by itself", not entry_guard.halted_today(date.today() + timedelta(days=1)) and entry_guard.count(date.today() + timedelta(days=1)) == 0)
check("a restart cannot forget it (state is on disk)", json.loads(paths.ENTRY_FAILS_FILE.read_text())["count"] == 2)
check("reset() forgets it", entry_guard.reset() is True and not entry_guard.halted_today() and entry_guard.reset() is False)
paths.ENTRY_FAILS_FILE.write_text("garbage")
check("a corrupt file counts as zero, not a crash", entry_guard.count() == 0)
fresh()

print("\n[2] control integration")
entry_guard.record_failure("RMS:Margin_Exceeds *bold* `tick`"); entry_guard.record_failure("again")
p, why = control.is_paused()
check("after 2 failures the bot reports itself paused, with the reason", p and "done for the day" in why and "2 entry attempts failed" in why, why)
check("the reason is safe inside a Telegram Markdown message", md_ok(f"⏸ *PAUSED* — {why}"), why)
check("/resume clears it (a fresh start) and says it changed something", control.resume() is True and not control.is_paused()[0] and entry_guard.count() == 0)
check("the owner's pause_all is still reported first when both apply", (control.pause_all(), control.is_paused()[1])[1].startswith("paused for ALL"))
fresh()

print("\n[3] main.py: refused orders (synchronous) inside a real scan")
import data.sr_database as _srdb                                   # noqa: E402
_srdb._DB_FILE = TMP / "sr.json"
import main                                                        # noqa: E402
import broker.angel as A                                           # noqa: E402
from data import option_chain as ocmod                             # noqa: E402
from signals.combiner import FinalSignal                           # noqa: E402
from signals.trade_monitor import TradeState                       # noqa: E402
from fakes_angel import FakeAngel, make_client, sym, opt_price, BY_SYMBOL, EXP1, SPOT, Resp   # noqa: E402
import random                                                       # noqa: E402

random.seed(4)
walk, px = [], SPOT - 150
base = datetime(2026, 10, 19, 9, 15)
for i in range(150):
    o = px; px += random.uniform(-14, 15); h = max(o, px) + random.uniform(0, 8); l = min(o, px) - random.uniform(0, 8)
    day, slot = divmod(i, 25)
    walk.append([(base + timedelta(days=day, minutes=15 * slot)).strftime("%Y-%m-%dT%H:%M:%S+0530"), o, h, l, px, 0])

posts = []
main._post = lambda t: posts.append(t) or True
main.send_error_alert = lambda t: None
main.send_signal = lambda *a, **k: None
main._is_market_open = lambda: True
main._entry_allowed = lambda: True
main._STATE_FILE = TMP / "main_state.json"


def scans(n, fail=True):
    f = FakeAngel()
    if fail:
        f.fail_place = "Insufficient funds"
    orig = f.request
    def req(method, url, headers=None, timeout=None, data=None):
        if url.endswith("getCandleData"):
            f.calls.append((url.replace(A.ROOT_URL, ""), json.loads(data), method, headers or {}))
            return Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": walk})
        return orig(method, url, headers=headers, timeout=timeout, data=data)
    f.request = req
    f.spot = walk[-1][4]
    cl, _, _ = make_client(f, paper=False)
    k = round(f.spot / 50) * 50 + 100
    s_ = sym(EXP1, k, "CE")
    fs = FinalSignal(action="CALL_SELL", strength="STRONG", score=3, lots=1, strike=k, symbol=s_, premium=opt_price(BY_SYMBOL[s_], f.spot),
                     sl_spot_level=f.spot + 60, expiry=EXP1.strftime("%d-%b-%Y"), spot_price=f.spot)
    main.combine_signals = lambda **kw: fs
    main._get_kite = lambda: cl
    saved = ocmod._fetch_with_retry
    ocmod._fetch_with_retry = lambda: (_ for _ in ()).throw(RuntimeError("NSE blocked"))
    attempts = []
    for _ in range(n):
        ocmod._kite_instruments_cache.clear(); ocmod._oi_snapshot.clear()
        main.monitor.clear_trade(); main.strangle_legs = None
        before = len([c for c in f.calls if c[0].endswith("placeOrder")])
        main.run_scan()
        attempts.append(len([c for c in f.calls if c[0].endswith("placeOrder")]) - before)
    ocmod._fetch_with_retry = saved
    return attempts, f


fresh(); posts.clear(); config.ENTRY_FAIL_LIMIT = 2
att, f = scans(4)
check("limit 2: scan 1 and 2 each try to place an order; scans 3 and 4 place NONE (done for the day)", att[0] >= 1 and att[1] >= 1 and att[2:] == [0, 0], str(att))
check("Telegram: 'attempt 1 of 2', then 'Done for the day', and no more after that",
      sum("Entry attempt 1 of 2" in m for m in posts) == 1 and sum("Done for the day" in m for m in posts) == 1 and not any("Entry attempt 3" in m for m in posts), str([m[:50] for m in posts]))
check("every message is valid Telegram Markdown", all(md_ok(m) for m in posts if "attempt" in m or "Done for the day" in m), str([m for m in posts if not md_ok(m)][:1]))
check("nothing is left open after the failures", main.monitor.trade is None)
fresh(); posts.clear(); config.ENTRY_FAIL_LIMIT = 0
att, f = scans(3)
check("limit 0 (the default): it keeps trying every scan, forever — behaviour unchanged", all(a >= 1 for a in att) and not any("Done for the day" in m for m in posts), str(att))
fresh(); posts.clear(); config.ENTRY_FAIL_LIMIT = 2
att, f = scans(1, fail=False)
check("a successful entry is not a failure and does not count", entry_guard.count() == 0 and main.monitor.trade is not None, str(att))
main.monitor.clear_trade()

print("\n[4] main.py: orders accepted, then rejected (asynchronous) — caught by the reconcile")
class FK:
    VARIETY_REGULAR = "regular"; EXCHANGE_NFO = "NFO"; TRANSACTION_TYPE_BUY = "BUY"; TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"; ORDER_TYPE_MARKET = "MARKET"
    def __init__(self, positions, orders): self.pos = positions; self.ords = orders; self.placed = []
    def positions(self): return {"net": [{"tradingsymbol": s, "quantity": q} for s, q in self.pos.items()], "day": []}
    def orders(self): return self.ords
    def ltp(self, keys): return {k: {"last_price": 10.0} for k in keys}
    def place_order(self, **kw): self.placed.append(kw); return f"X{len(self.placed)}"


short_s, hedge_s = sym(EXP1, 23000, "CE"), sym(EXP1, 23700, "CE")


def trade(age_min):
    t = TradeState(action="CALL_SELL", strike=23000, symbol=short_s, entry_time=datetime.now() - timedelta(minutes=age_min), entry_premium=90.0,
                   entry_spot=SPOT, sl_spot_level=SPOT + 60, expiry=EXP1.strftime("%d-%b-%Y"), lots=65, hedge_symbol=hedge_s, hedge_strike=23700, hedge_entry_premium=5.0)
    main.monitor.clear_trade(); main.strangle_legs = None; main._ext_close_strikes.clear()
    main.monitor.set_trade(t)
    return t


journaled = []
main._journal_external_close = lambda kite, t, leg: journaled.append(leg["symbol"])


def reconcile_twice(kite):
    return [main._reconcile_open_trade(kite) for _ in range(2)]


rej = [{"order_id": "1", "tradingsymbol": short_s, "transaction_type": "SELL", "status": "REJECTED", "order_timestamp": datetime.now()}]
fresh(); posts.clear(); journaled.clear(); config.ENTRY_FAIL_LIMIT = 2
trade(1); kite = FK({hedge_s: 65}, rej)             # hedge filled, the short leg was rejected: a naked long hedge is left behind
r = reconcile_twice(kite)
check("fresh entry, short leg REJECTED after acceptance: 1st check FLAT (no orders), 2nd CLOSED", r == ["FLAT", "CLOSED"], str(r))
check("the orphan long hedge is SOLD back", [p["tradingsymbol"] for p in kite.placed] == [hedge_s] and kite.placed[0]["transaction_type"] == "SELL", str(kite.placed))
check("it is counted as ONE failed entry, the phantom trade is gone, and NO manual-exit journal entry was written",
      entry_guard.count() == 1 and main.monitor.trade is None and journaled == [], str((entry_guard.count(), journaled)))
check("Telegram explains: cleaned up + attempt 1 of 2", any("Cleaned up" in m for m in posts) and any("attempt 1 of 2" in m for m in posts) and all(md_ok(m) for m in posts), str([m[:60] for m in posts]))

fresh(); posts.clear(); journaled.clear()
trade(1); kite = FK({}, rej); reconcile_twice(kite)
trade(1); kite = FK({}, rej); reconcile_twice(kite)
check("two such rejections in a day halt the bot for the day", entry_guard.halted_today() and control.is_paused()[0] and any("Done for the day" in m for m in posts))
check("a rejected entry that left nothing behind places no orders", kite.placed == [])

done = [{"order_id": "1", "tradingsymbol": short_s, "transaction_type": "SELL", "status": "COMPLETE", "order_timestamp": datetime.now()}]
fresh(); posts.clear(); journaled.clear()
trade(2); kite = FK({}, done); r = reconcile_twice(kite)
check("a position that DID fill and was then closed by hand within minutes is a manual close, not a failed entry", r == ["FLAT", "CLOSED"] and entry_guard.count() == 0 and journaled == [short_s], str((r, entry_guard.count(), journaled)))
fresh(); journaled.clear()
trade(60); kite = FK({}, rej); reconcile_twice(kite)
check("an hour-old trade that vanished is the old manual-close path", entry_guard.count() == 0 and journaled == [short_s])
fresh(); journaled.clear(); config.ENTRY_FAIL_LIMIT = 0
trade(1); kite = FK({hedge_s: 65}, rej); reconcile_twice(kite)
check("limit 0: the reconcile behaves exactly as before (journals it, places nothing, counts nothing)", journaled == [short_s] and kite.placed == [] and entry_guard.count() == 0)

print("\n[5] provisioning and the guide")
import userprofile                                                  # noqa: E402
from notifications import onboarding                                # noqa: E402
t = userprofile.render_template("Rahul", "angel", 200000, 2000, "new", 1, paper=False, allow_pyramiding=False, owner=True, loss_per_lot=2000, max_failed_entries=2)
st = userprofile.build(userprofile.parse(t), {"MAX_START_LOTS": 3, "ENTRY_FAIL_LIMIT": 0}, t)
check("[overrides] ENTRY_FAIL_LIMIT = 2 round-trips into the settings", st.config_values["ENTRY_FAIL_LIMIT"] == 2, str(st.config_values.get("ENTRY_FAIL_LIMIT")))
check("the Telegram guide states the rule and is valid Markdown", "2* entry attempts fail" in onboarding.params_message(st) and md_ok(onboarding.params_message(st)))
t0 = userprofile.render_template("Priya", "angel", 500000)
st0 = userprofile.build(userprofile.parse(t0), {"MAX_START_LOTS": 3, "ENTRY_FAIL_LIMIT": 0}, t0)
check("without the flag the guide says nothing about it", "entry attempts fail" not in onboarding.params_message(st0))

import importlib.util, io, contextlib                                # noqa: E402
spec = importlib.util.spec_from_file_location("provision_user", ROOT / "scripts" / "provision_user.py")
PU = importlib.util.module_from_spec(spec); spec.loader.exec_module(PU)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = PU.main(["efuser", "--broker", "angel", "--live", "--lots", "1", "--capital", "200000", "--max-loss", "2000", "--loss-per-lot", "2000",
                  "--max-failed-entries", "2", "--base", str(TMP / "users"), "--no-seed"])
prof = (TMP / "users" / "efuser" / "profile.toml").read_text() if rc == 0 else ""
check("provision_user.py --max-failed-entries 2 succeeds and writes [overrides] ENTRY_FAIL_LIMIT = 2", rc == 0 and "ENTRY_FAIL_LIMIT = 2" in prof and "[overrides]" in prof, buf.getvalue()[-300:])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
