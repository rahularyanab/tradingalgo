"""
Offline tests for the X poster (publish/x_poster.py). Nothing is ever sent to X: the poster is
given a fake send function, every file lives in a temp dir, and the clock is simulated.

Run from the repo root:   python3 tests/test_x_poster.py          (add -v to print every post)
"""
import json, os, sys, tempfile, urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, x_poster as xp
from signals.trade_monitor import TradeState

VERBOSE = "-v" in sys.argv
TMP = Path(tempfile.mkdtemp())
LOT = feed.NIFTY_LOT_SIZE
IST = xp.IST
PASS, FAIL = [], []
CLOCK = [datetime(2026, 11, 27, 8, 0, tzinfo=IST)]
feed._now = lambda: CLOCK[0]                     # feed events are stamped with the simulated clock
xp.HOLIDAYS_FILE = TMP / "holidays.txt"
xp.HOLIDAYS_FILE.write_text("2026-12-25  # Christmas\n")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


SENT = []


def fake_send(text, creds):
    SENT.append((CLOCK[0].strftime("%H:%M"), text))
    return True, False, f"id{len(SENT)}"


def reset(tag):
    feed.OUTBOX = xp.OUTBOX = TMP / f"{tag}_outbox.jsonl"
    feed.STATE = TMP / f"{tag}_state.json"
    xp.CURSOR = TMP / f"{tag}_cursor.json"
    xp.DRYRUN_LOG = TMP / f"{tag}_dry.log"
    feed._mark = None
    SENT.clear()


def at(y, m, d, hh, mm):
    CLOCK[0] = datetime(y, m, d, hh, mm, tzinfo=IST)


def tick(poster=fake_send, live=True):
    return xp.run_once({}, live, poster=poster, now=CLOCK[0])


def texts():
    return [t for _, t in SENT]


def directional(lots=6, strike=24500, action="CALL_SELL"):
    leg = "CE" if action == "CALL_SELL" else "PE"
    return TradeState(action=action, strike=strike, symbol=f"NIFTY{strike}{leg}", entry_time=CLOCK[0],
                      entry_premium=100.0, entry_spot=24300, sl_spot_level=24450 if leg == "CE" else 24150,
                      expiry="2026-12-01", lots=lots * LOT, hedge_symbol=f"NIFTY24800{leg}", hedge_strike=24800,
                      hedge_entry_premium=10.0)


def scan(action="CALL_SELL", score=2):
    feed.scan_result(NS(action=action, strength="MODERATE", score=score, pcr=0.82, resistance_level=24450.4,
                        support_level=24100.2, call_wall=24500, put_wall=24000), 24312.5)


def snapshot(realised, trade, label="HOURLY"):
    feed.pnl_snapshot(realised, trade, label=label)


# ── 1. OAuth 1.0a signature, against golden values produced by oauthlib ──────────
print("oauth signing")
creds = {"api_key": "ck123", "api_secret": "cs/+=456", "access_token": "t-789", "access_secret": "ts&%abc"}


def sig(extra):
    h = xp.oauth_header("POST", "https://api.x.com/2/tweets", creds, extra_params=extra,
                        nonce="abcNONCE123", timestamp="1700000000")
    return urllib.parse.unquote([p for p in h.split(", ") if p.startswith("oauth_signature=")][0].split('"')[1])


check("signature = oauthlib's (JSON post, no params)", sig({}) == "NpZ24zXuLnVyheOQP1y3IcFQUp4=", sig({}))
q = {"q": "₹ 24,500 CE ~ * ' ( ) !"}
check("signature = oauthlib's (unicode + reserved characters)", sig(q) == "sY+6vxJk5+IWyp+1+szr9My1YBg=", sig(q))

# ── 2. a normal day: the plan Rahul asked for ─────────────────────────────────
print("normal day (Fri 27 Nov)")
reset("normal")
at(2026, 11, 27, 8, 50); tick()                                  # poster already running before 9:00
check("nothing before 9:00", SENT == [])
at(2026, 11, 27, 9, 0); tick()
at(2026, 11, 27, 9, 5); tick()
check("9:00 greeting, once, explains the fixed sizes", len(SENT) == 1 and SENT[0][0] == "09:00" and "Follow @ThetaDeskIN" in SENT[0][1]
      and "Size: 3 lots strong · 2 moderate · 1/leg strangle" in SENT[0][1] and SENT[0][1].endswith(xp.HASHTAGS), SENT)
check("greeting names the Telegram channel as plain text (no link fee, no @-tag of a stranger)",
      "Real-time + 1-lot alerts: ThetaDeskIN on Telegram" in SENT[0][1] and "t.me" not in SENT[0][1]
      and "@ThetaDeskINbot" not in SENT[0][1] and "…" not in SENT[0][1], SENT[0][1])
check("_fit: dropping hashtags is enough → no stray '…'", xp._fit("a" * 270 + "\n" + "#x" * 10) == "a" * 270)
check("_fit: still too long → cut to 280 with '…'", len(xp._fit("x" * 300)) == 280 and xp._fit("x" * 300).endswith("…"))
at(2026, 11, 27, 9, 15); scan(); tick()
at(2026, 11, 27, 9, 30); scan("STRANGLE"); tick()                # second pre-10:00 scan: not posted
check("9:15 first scan, once", len(SENT) == 2 and "First scan 09:15" in SENT[1][1]
      and "Bearish — favours selling calls (2/3)" in SENT[1][1] and "trading starts 10:00 sharp" in SENT[1][1], SENT[-1])
check("scan shows no strike or premium", "24500 CE" not in SENT[1][1] and "₹" not in SENT[1][1])
at(2026, 11, 27, 10, 0); t = directional(6); feed.trade_entry(t, 24300, strength="STRONG"); tick()
check("10:00 entry posted at the MODEL size: strong = 3 lots (owner filled 6)",
      len(SENT) == 3 and SENT[2][0] == "10:00" and "Sold 3 lots 24500 CE @ ₹100.00" in SENT[2][1]
      and "strong signal" in SENT[2][1] and "Hedge: bought 3 lots 24800 CE" in SENT[2][1], SENT[-1])
at(2026, 11, 27, 10, 15); feed.mark(-650, 24310); snapshot(0, t); tick()
check("10:15 hourly skipped (trade post 15 min ago)", len(SENT) == 3)
at(2026, 11, 27, 10, 40); t.lots = 12 * LOT; t.entry_premium = 95.0
feed.trade_add(t, 6 * LOT, [{"leg": "CE", "strike": 24500, "price": 90.0}]); tick()
check("pyramid add: no post", len(SENT) == 3)
at(2026, 11, 27, 11, 15); feed.mark(13000, 24250); snapshot(0, t); tick()
check("11:15 hourly at model size (13000/6 × 3)", len(SENT) == 4 and "Day P&L: +₹6,500" in SENT[3][1], SENT[-1])
at(2026, 11, 27, 11, 40); feed.trade_exit(replace(t, lots=4 * LOT), 60.0, 9100.0, "PROFIT_BOOK", 24250); tick()
check("partial booking: no post", len(SENT) == 4)
at(2026, 11, 27, 12, 15); feed.mark(9000, 24255); snapshot(9100, t); tick()
check("12:15 hourly: booked part of the open trade + open, model size",
      "Day P&L: +₹9,050" in SENT[-1][1] and "Booked +₹4,550 · Open +₹4,500" in SENT[-1][1], SENT[-1])
at(2026, 11, 27, 12, 50); t.lots = 8 * LOT; feed.trade_exit(t, 70.0, 13000.0, "SIGNAL_EXIT", 24260); tick()
check("12:50 close posted: whole trade's result at 3 lots (22100/6 × 3)",
      SENT[-1][0] == "12:50" and "TRADE CLOSED" in SENT[-1][1] and "Trade result: +₹11,050 (3 lots)" in SENT[-1][1], SENT[-1])
at(2026, 11, 27, 13, 15); snapshot(22100, None); tick()
check("13:15 hourly skipped (25 min after the close post)", SENT[-1][0] == "12:50")
for hh in (14, 15):
    at(2026, 11, 27, hh, 15); snapshot(22100, None); tick()
at(2026, 11, 27, 15, 35); snapshot(22100, None, label="CLOSE"); tick()
close = SENT[-1][1]
check("15:35 day close with stats", "Day closed" in close and "Day P&L: +₹11,050" in close
      and "Trades: 1 · ✅ 1 · ❌ 0" in close and "Nov so far: +₹11,050 (1 day)" in close, close)
check("whole day = 9 posts, under the cap of 10", len(SENT) == 9, [s[0] for s in SENT])
check("post times", [s[0] for s in SENT] == ["09:00", "09:15", "10:00", "11:15", "12:15", "12:50", "14:15", "15:15", "15:35"],
      [s[0] for s in SENT])
check("owner's real ₹ amounts never posted", not any(x in s for s in texts() for x in ("9,100", "13,000", "22,100")))
check("owner's real lot counts never posted", not any(x in s for s in texts() for x in ("6 lots", "12 lots", "4 lots")))
check("no links", not any("http" in s or "www." in s for s in texts()))
check("every post fits in 280", all(xp._weight(s) <= 280 for s in texts()), [xp._weight(s) for s in texts()])
if VERBOSE:
    print("\n" + "\n\n".join(f"[{a}]\n{b}" for a, b in SENT) + "\n")

# ── 3. a busy day: the cap holds, nothing is left dangling ───────────────────
print("busy day: 6 round trips")
reset("busy")
at(2026, 11, 30, 8, 59); tick()
at(2026, 11, 30, 9, 0); tick()
at(2026, 11, 30, 9, 15); scan(); tick()
entries_posted = 0
for i, hh in enumerate((10, 11, 12, 13, 14, 15)):
    at(2026, 11, 30, hh, 0); t = directional(4, strike=24500 + 50 * i); feed.trade_entry(t, 24300); tick()
    at(2026, 11, 30, hh, 15); feed.mark(1000, 24300); snapshot(0, t); tick()
    at(2026, 11, 30, hh, 50); feed.trade_exit(t, 80.0, 5200.0 if i % 2 == 0 else -2600.0, "SL_HIT" if i % 2 else "TARGET", 24300); tick()
at(2026, 11, 30, 15, 35); snapshot(0, None, label="CLOSE"); tick()
ts_ = texts()
n_entries = sum("NEW TRADE" in s for s in ts_)
n_exits = sum("TRADE CLOSED" in s for s in ts_)
check("never more than 10 posts", len(SENT) <= 10, len(SENT))
check("every announced entry has its exit", n_entries == n_exits and n_entries >= 1, (n_entries, n_exits))
check("day close still posted, last", "Day closed" in ts_[-1])
check("close counts ALL 6 trades, even unannounced ones", "Trades: 6 · ✅ 3 · ❌ 3" in ts_[-1], ts_[-1])
check("unknown grade → 2 lots; day = sum of trades (3×2600 − 3×1300)", "Day P&L: +₹3,900" in ts_[-1]
      and "Best +₹2,600 · Worst −₹1,300" in ts_[-1], ts_[-1])
check("busy day posts nothing beyond what fits", len(SENT) == 2 + 2 * n_entries + 1 + sum("⏱" in s for s in ts_))
if VERBOSE:
    print("\n" + "\n\n".join(f"[{a}]\n{b}" for a, b in SENT) + "\n")

# ── 3b. model size ignores what the owner actually got filled ──────────────────
print("model size, not the owner's fills")
reset("model")
at(2026, 12, 4, 8, 0); tick()
at(2026, 12, 4, 10, 0); t = directional(2); feed.trade_entry(t, 24300, strength="STRONG"); tick()   # margin: 6 → 2 lots
at(2026, 12, 4, 11, 0); feed.trade_exit(t, 80.0, 2600.0, "TARGET", 24200); tick()
check("STRONG signal filled at only 2 lots: X still shows 3 lots", "Sold 3 lots 24500 CE" in SENT[0][1], SENT[0][1])
check("…and the result scales to 3 lots (2600/2 × 3)", "Trade result: +₹3,900 (3 lots)" in SENT[1][1], SENT[1][1])
at(2026, 12, 4, 14, 10); t = directional(1, action="PUT_SELL"); feed.trade_entry(t, 24300, strength="MODERATE"); tick()  # 14:00 half size
at(2026, 12, 4, 14, 50); feed.trade_exit(t, 80.0, -650.0, "SL_HIT", 24200); tick()
check("MODERATE signal cut to 1 lot by the 14:00 rule: X shows 2 lots", "Sold 2 lots 24500 PE" in SENT[2][1]
      and "Stop: spot below" in SENT[2][1], SENT[2][1])
check("…loss scales to 2 lots (−650/1 × 2)", "Trade result: −₹1,300 (2 lots)" in SENT[3][1], SENT[3][1])
s_ = TradeState(action="STRANGLE", strike=24600, symbol="x", entry_time=CLOCK[0], entry_premium=80.0, entry_spot=24300,
                sl_spot_level=24650, sl_put=23950, expiry="2026-12-08", lots=4 * LOT)
reset("strangle")
at(2026, 12, 4, 8, 0); tick()
at(2026, 12, 4, 10, 0)
feed.trade_entry(s_, 24300, strength="STRANGLE", strangle={"ce_strike": 24600, "ce_symbol": "C", "ce_price": 45.0,
                                                          "pe_strike": 24000, "pe_symbol": "P", "pe_price": 35.0}); tick()
at(2026, 12, 4, 11, 0); feed.trade_exit(replace(s_, action="STRANGLE_CE", strike=24600), 70.0, -6500.0, "SL_HIT", 24660); tick()
at(2026, 12, 4, 15, 20); feed.trade_exit(replace(s_, action="STRANGLE_PE", strike=24000), 5.0, 7800.0, "EOD", 24640); tick()
check("strangle: 1 lot each leg (3/2/1)", "Sold 1 lot each: 24600 CE @ ₹45.00 + 24000 PE @ ₹35.00" in SENT[0][1], SENT[0][1])
check("first leg's stop-out is not a separate post", len(SENT) == 2, [t[:40] for t in texts()])
check("strangle result nets both legs at 1 lot ((−6500+7800)/4 × 1)", "Trade result: +₹325 (1 lot each leg)" in SENT[1][1]
      and "Nifty strangle, last leg 24000 PE" in SENT[1][1], SENT[1][1])
check("all posts fit in 280", all(xp._weight(x) <= 280 for x in texts()))
if VERBOSE:
    print("\n" + "\n\n".join(f"[{a}]\n{b}" for a, b in SENT) + "\n")

# ── 4. no-trade day, holiday, weekend ────────────────────────────────────────
print("quiet days")
reset("notrade")
at(2026, 12, 1, 9, 0); tick()
at(2026, 12, 1, 9, 15); scan("NO_SIGNAL", 0); tick()
for hh in (10, 11, 12, 13, 14, 15):
    at(2026, 12, 1, hh, 15); snapshot(0, None); tick()
at(2026, 12, 1, 15, 35); snapshot(0, None, label="CLOSE"); tick()
check("no-trade day: greeting, scan, close — 3 posts", len(SENT) == 3, [s[0] for s in SENT])
check("scan says no clear edge", "No clear edge yet" in SENT[1][1], SENT[1][1])
check("close explains the day out", "No trade today" in SENT[2][1] and "Dec so far: +₹0 (1 day)" in SENT[2][1], SENT[2][1])

reset("holiday")
at(2026, 12, 25, 9, 0); tick()
at(2026, 12, 25, 9, 15); scan(); tick()
at(2026, 12, 25, 15, 35); snapshot(0, None, label="CLOSE"); tick()
check("NSE holiday (25 Dec): nothing posted", SENT == [])

reset("weekend")
at(2026, 11, 28, 9, 0); tick()
check("Saturday: no greeting", SENT == [])

# ── 5. restarts, late starts, old events ─────────────────────────────────────
print("restarts")
reset("first")
at(2026, 12, 2, 10, 0); feed.trade_entry(directional(), 24300)
at(2026, 12, 2, 10, 5); tick()
check("first start replays nothing", SENT == [] and json.loads(xp.CURSOR.read_text())["seq"] == 1)
reset("late")
at(2026, 12, 2, 9, 20); tick()
check("poster started after 9:15: no greeting", SENT == [])
at(2026, 12, 2, 9, 50); scan(); tick()
check("scan after 9:45: not posted as 'first scan'", SENT == [])
reset("yday")
at(2026, 12, 2, 8, 0); tick()
at(2026, 12, 2, 14, 0); t = directional(); feed.trade_entry(t, 24300)
at(2026, 12, 3, 9, 30); tick()                                    # poster was down overnight
check("yesterday's event is not posted today", SENT == [])

# ── 6. retries and month totals ──────────────────────────────────────────────
print("retries")
reset("retry")
at(2026, 12, 3, 8, 0); tick()
at(2026, 12, 3, 10, 0); t = directional(); feed.trade_entry(t, 24300); tick()
at(2026, 12, 3, 11, 0); feed.trade_exit(t, 50.0, 19500.0, "TARGET", 24200)
calls = {"n": 0}


def flaky(text, c):
    calls["n"] += 1
    if calls["n"] == 1:
        return False, True, "HTTP 429"
    return fake_send(text, c)


tick(poster=flaky)
check("429: nothing counted, cursor not advanced", len(SENT) == 1 and json.loads(xp.CURSOR.read_text())["seq"] == 1)
tick(poster=flaky)
check("retry posts once, result not double counted", len(SENT) == 2 and "+₹6,500 (2 lots)" in SENT[1][1], SENT[-1])
check("post counter = 2", json.loads(xp.CURSOR.read_text())["day"]["posts"] == 2)

reset("month")
for d, pnl in ((7, 6500.0), (8, -1300.0)):
    at(2026, 12, d, 8, 0); tick()
    at(2026, 12, d, 10, 0); t = directional(); feed.trade_entry(t, 24300); tick()
    at(2026, 12, d, 12, 0); feed.trade_exit(t, 50.0, pnl, "TARGET", 24200); tick()
    at(2026, 12, d, 15, 35); snapshot(pnl, None, label="CLOSE"); tick()
# 6500/6×2 = 2166.67 → posted +₹2,167; −1300/6×2 = −433.33 → posted −₹433; month = +₹1,734 (what a follower can add up)
check("month so far = the sum of the posted day totals", "Dec so far: +₹1,734 (2 days)" in SENT[-1][1], SENT[-1][1])

reset("dry")
at(2026, 12, 9, 9, 0); xp.run_once({}, False, poster=fake_send, now=CLOCK[0])
check("dry run: nothing sent, text in the log, counted toward the cap",
      SENT == [] and "Good morning" in xp.DRYRUN_LOG.read_text()
      and json.loads(xp.CURSOR.read_text())["day"]["posts"] == 1)

# ── 7. credentials ────────────────────────────────────────────────────────────
print("credentials")
xp.ENV_FILE = TMP / "x.env"
for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_LIVE"):
    os.environ.pop(k, None)
xp.ENV_FILE.write_text("X_API_KEY=a\nX_API_SECRET=b\nX_ACCESS_TOKEN=c\nX_ACCESS_SECRET=d\n")
check("keys present but X_LIVE unset → dry run", xp.load_env()[1] is False)
xp.ENV_FILE.write_text("X_API_KEY=a\nX_API_SECRET=b\nX_ACCESS_TOKEN=c\nX_ACCESS_SECRET=\nX_LIVE=1\n")
check("X_LIVE=1 with a missing key → still dry run", xp.load_env()[1] is False)
xp.ENV_FILE.write_text("X_API_KEY=a\nX_API_SECRET=b\nX_ACCESS_TOKEN=c\nX_ACCESS_SECRET=d\nX_LIVE=1\n")
check("all keys + X_LIVE=1 → live", xp.load_env()[1] is True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
