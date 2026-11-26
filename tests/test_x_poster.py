"""
Offline tests for the X poster (publish/x_poster.py). Nothing is ever sent to X: the poster is
given a fake send function, and every file lives in a temp dir.

Run from the repo root:   python3 tests/test_x_poster.py          (add -v to print every post)
"""
import json, os, sys, tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, x_poster as xp
from signals.trade_monitor import TradeState

VERBOSE = "-v" in sys.argv
TMP = Path(tempfile.mkdtemp())
LOT = feed.NIFTY_LOT_SIZE
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def reset(tag):
    feed.OUTBOX = xp.OUTBOX = TMP / f"{tag}_outbox.jsonl"
    feed.STATE = TMP / f"{tag}_state.json"
    xp.CURSOR = TMP / f"{tag}_cursor.json"
    xp.DRYRUN_LOG = TMP / f"{tag}_dry.log"
    feed._mark = None


SENT = []


def fake_send(text, creds):
    SENT.append(text)
    return True, False, f"id{len(SENT)}"


def start_poster():
    """Simulate the poster having been running already (cursor at the current end)."""
    xp.run_once({}, True, poster=fake_send)


def weight(s):
    return sum(2 if ord(c) > 0x2000 else 1 for c in s)


def directional(lots=6):
    return TradeState(action="CALL_SELL", strike=24500, symbol="NIFTY24500CE", entry_time=datetime.now(),
                      entry_premium=100.0, entry_spot=24300, sl_spot_level=24450, expiry="2026-12-01",
                      lots=lots * LOT, hedge_symbol="NIFTY24800CE", hedge_strike=24800, hedge_entry_premium=10.0)


# ── 1. OAuth 1.0a signature, against golden values produced by oauthlib (the reference Python OAuth
#       library), with awkward secrets and characters (₹ + / & % ~ * ' ( ) !) ─────────────────────
print("oauth signing")
import urllib.parse
creds = {"api_key": "ck123", "api_secret": "cs/+=456", "access_token": "t-789", "access_secret": "ts&%abc"}


def sig(extra):
    h = xp.oauth_header("POST", "https://api.x.com/2/tweets", creds, extra_params=extra,
                        nonce="abcNONCE123", timestamp="1700000000")
    return urllib.parse.unquote([p for p in h.split(", ") if p.startswith("oauth_signature=")][0].split('"')[1])


check("signature = oauthlib's (JSON post, no params)", sig({}) == "NpZ24zXuLnVyheOQP1y3IcFQUp4=", sig({}))
q = {"q": "₹ 24,500 CE ~ * ' ( ) !"}
check("signature = oauthlib's (unicode + reserved characters)", sig(q) == "sY+6vxJk5+IWyp+1+szr9My1YBg=", sig(q))
check("header carries every oauth field",
      all(f in xp.oauth_header("POST", "u", creds) for f in ("oauth_consumer_key", "oauth_nonce", "oauth_signature=",
          "oauth_signature_method=\"HMAC-SHA1\"", "oauth_timestamp", "oauth_token", "oauth_version=\"1.0\"")))

# ── 2. a full directional day, through the real feed ──────────────────────────
print("directional day")
reset("dir"); SENT.clear()
start_poster()
t = directional(6)
feed.trade_entry(t, 24300)
t.lots = 12 * LOT; t.entry_premium = 95.0
feed.trade_add(t, 6 * LOT, [{"leg": "CE", "strike": 24500, "price": 90.0}])
feed.mark(-1300.0, 24320)
feed.pnl_snapshot(0.0, t)
feed.trade_exit(replace(t, lots=4 * LOT), 60.0, 9100.0, "PROFIT_BOOK", 24250)
feed.trade_exit(replace(t, lots=8 * LOT), 70.0, 13000.0, "SIGNAL_EXIT", 24260)
feed.pnl_snapshot(22100.0, None, label="CLOSE")
n = xp.run_once({}, True, poster=fake_send)
check("6 events → 6 posts", n == 6 and len(SENT) == 6, len(SENT))
entry, add, snap, book, close, day = SENT
check("entry: sold strike, price, spot, stop direction, hedge",
      "Sold 24500 CE @ ₹100.00" in entry and "Stop: spot above 24,450" in entry and "Hedge: bought 24800 CE" in entry, entry)
check("add shows size as a multiple of the start, not real lots", "size now 2.0× start" in add and "12" not in add, add)
check("hourly shows per-lot numbers", "Day P&L: −₹217 per lot" in snap, snap)
check("partial exit: share of position closed", "EXIT (part)" in book and "Closed 33% of the position" in book, book)
check("final exit sums all parts per lot (9100+13000)/6", "Trade result: +₹3,683 per lot" in close, close)
check("closing line", "Day closed" in day and "Day P&L: +₹3,683 per lot" in day and "1 trade closed today" in day, day)
check("owner's ₹ amounts never posted", not any("9,100" in s or "13,000" in s or "22,100" in s for s in SENT))
check("no links in any post", not any("http" in s or "www." in s for s in SENT))
check("every post fits in 280", all(weight(s) <= 280 for s in SENT), [weight(s) for s in SENT])
if VERBOSE:
    print("\n" + "\n\n".join(SENT) + "\n")

# ── 3. strangle ───────────────────────────────────────────────────────────────
print("strangle")
reset("str"); SENT.clear()
start_poster()
s = TradeState(action="STRANGLE", strike=24600, symbol="x", entry_time=datetime.now(), entry_premium=80.0,
               entry_spot=24300, sl_spot_level=24650, sl_put=23950, expiry="2026-12-01", lots=4 * LOT)
feed.trade_entry(s, 24300, strangle={"ce_strike": 24600, "ce_symbol": "C", "ce_price": 45.0,
                                     "pe_strike": 24000, "pe_symbol": "P", "pe_price": 35.0})
feed.trade_exit(replace(s, action="STRANGLE_CE", strike=24600), 70.0, -6500.0, "SL_HIT", 24660)
feed.trade_exit(replace(s, action="STRANGLE_PE", strike=24000), 5.0, 7800.0, "EOD", 24640)
xp.run_once({}, True, poster=fake_send)
check("strangle entry lists both legs and both stops",
      "24600 CE @ ₹45.00 + 24000 PE @ ₹35.00" in SENT[0] and "above 24,650 / below 23,950" in SENT[0], SENT[0])
check("leg stop-loss posted as a loss", SENT[1].startswith("❌") and "Stop-loss hit" in SENT[1], SENT[1])
check("strangle result nets both legs per lot", "Trade result: +₹325 per lot" in SENT[2], SENT[2])
if VERBOSE:
    print("\n" + "\n\n".join(SENT) + "\n")

# ── 4. safety rules ───────────────────────────────────────────────────────────
print("safety")
reset("first"); SENT.clear()
feed.trade_entry(directional(), 24300)
feed.trade_exit(directional(), 50.0, 19500.0, "TARGET", 24200)
check("first start replays nothing", xp.run_once({}, True, poster=fake_send) == 0 and SENT == [])
check("…and the cursor sits at the end", json.loads(xp.CURSOR.read_text())["seq"] == 2)

reset("hol"); SENT.clear(); start_poster()
feed.pnl_snapshot(0.0, None)
feed.pnl_snapshot(0.0, None, label="CLOSE")
xp.run_once({}, True, poster=fake_send)
check("no trade, flat (or holiday): nothing posted", SENT == [])

reset("stale"); SENT.clear(); start_poster()
feed.trade_entry(directional(), 24300)
feed.mark(650.0, 24290)
feed.pnl_snapshot(0.0, directional())
later = datetime.now().astimezone() + timedelta(minutes=45)
xp.run_once({}, True, poster=fake_send, now=later)
check("late entry still posted, marked delayed; stale hourly line skipped",
      len(SENT) == 1 and "(delayed)" in SENT[0], SENT)

reset("retry"); SENT.clear(); start_poster()
feed.trade_entry(directional(), 24300)
feed.trade_exit(directional(), 50.0, 19500.0, "TARGET", 24200)
calls = {"n": 0}


def flaky(text, creds):
    calls["n"] += 1
    if calls["n"] == 2:                     # the exit fails once with a rate limit
        return False, True, "HTTP 429"
    return fake_send(text, creds)


xp.run_once({}, True, poster=flaky)
check("rate limit: stops, cursor stays before the failed event", len(SENT) == 1 and json.loads(xp.CURSOR.read_text())["seq"] == 1)
xp.run_once({}, True, poster=flaky)
check("retry posts it once, per-lot total not double counted", len(SENT) == 2 and "+₹3,250 per lot" in SENT[1], SENT[-1])

reset("refuse"); SENT.clear(); start_poster()
feed.trade_entry(directional(), 24300)
feed.trade_exit(directional(), 50.0, 19500.0, "TARGET", 24200)
xp.run_once({}, True, poster=lambda t, c: (False, False, "HTTP 403 duplicate"))
check("permanent refusal: logged and skipped, poster not stuck", json.loads(xp.CURSOR.read_text())["seq"] == 2)

reset("dry"); SENT.clear(); start_poster()
feed.trade_entry(directional(), 24300)
xp.run_once({}, False, poster=fake_send)
check("dry run: nothing sent, exact text written to the dry-run log",
      SENT == [] and "Sold 24500 CE @ ₹100.00" in xp.DRYRUN_LOG.read_text())

# ── 5. credentials ────────────────────────────────────────────────────────────
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
