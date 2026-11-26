"""
Offline tests for the Telegram service (publish/telegram_live.py) and the shared whole-lot ledger
(publish/follow.py). Nothing reaches Telegram or Zerodha: both are faked, the clock is simulated,
and every file lives in a temp dir.

The main scenario replays Rahul's REAL trades of 26 Nov 2026 (both entries were margin-cut to 2 lots):
  10:30 CALL_SELL 26000 MODERATE, 2 lots @167.80 → 12:00 trailing lock, net −₹812
  12:15 CALL_SELL 26000 STRONG,   2 lots @180.00 → 14:00 book 1 lot +₹5,346 → 14:45 last lot +₹1,732
Run from the repo root:   python3 tests/test_telegram_live.py          (add -v to print messages)
"""
import json, os, sys, tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, x_poster as X, telegram_live as T, follow as F
from signals.trade_monitor import TradeState

VERBOSE = "-v" in sys.argv
TMP = Path(tempfile.mkdtemp())
LOT = feed.NIFTY_LOT_SIZE
IST = X.IST
PASS, FAIL = [], []
CLOCK = [datetime(2026, 11, 27, 8, 0, tzinfo=IST)]
feed._now = lambda: CLOCK[0]
X.now_ist = lambda: CLOCK[0]                     # the bot's access clock follows the simulated clock too
X.DELAY_MIN = 0                                  # X's own 15-min delay is tested in test_x_poster.py
X.HOLIDAYS_FILE = TMP / "holidays.txt"
X.HOLIDAYS_FILE.write_text("2026-12-25\n")
CH = "@ThetaDeskLive"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


class FakeTG(T.TG):
    def __init__(self):
        super().__init__("TEST")
        self.username = "ThetaDeskBot"
        self.sent, self.fail_next = [], {}

    def call(self, method, **p):
        if method == "sendMessage":
            chat = str(p["chat_id"])
            if self.fail_next.get(chat):
                code = self.fail_next[chat].pop(0)
                if not self.fail_next[chat]:
                    del self.fail_next[chat]
                return {"ok": False, "error_code": code, "description": "fake"}
            self.sent.append((CLOCK[0].strftime("%H:%M"), chat, p["text"], p.get("reply_markup")))
        return {"ok": True, "result": {}}


OWNER = "900"


def reset(tag, live=True, delay=0):
    feed.OUTBOX = T.OUTBOX = TMP / f"{tag}_outbox.jsonl"
    feed.STATE = TMP / f"{tag}_feed.json"
    T.STATE, T.SUBS, T.DRYRUN_LOG = TMP / f"{tag}_tg.json", TMP / f"{tag}_subs.json", TMP / f"{tag}_dry.log"
    feed._mark = None
    tg = FakeTG()
    return tg, T.Publisher(tg, {"channel": CH, "live": live, "token": "TEST", "delay_min": delay, "owner_id": OWNER})


def at(d, hh, mm):
    CLOCK[0] = datetime(2026, 11, d, hh, mm, tzinfo=IST) if d > 0 else CLOCK[0]


def msgs(tg, chat):
    """What the user reads: the HTML-escaped text as sent, with &amp; shown as &."""
    return [m[2].replace("&amp;", "&") for m in tg.sent if m[1] == chat]


def call_sell(lots, strike=26000, prem=180.0, hedge=27050, hprem=5.0, sl=26050):
    return TradeState(action="CALL_SELL", strike=strike, symbol=f"NIFTY26D01{strike}CE", entry_time=CLOCK[0],
                      entry_premium=prem, entry_spot=25945, sl_spot_level=sl, expiry="01-Dec-2026",
                      lots=lots * LOT, hedge_symbol=f"NIFTY26D01{hedge}CE", hedge_strike=hedge, hedge_entry_premium=hprem)


def subscribe(chat, plan, name="A"):
    T.reply_for("/start", chat, name, {"channel": CH})
    T.on_callback(f"plan:{plan}", chat)


# ── 1. the shared ledger on its own ───────────────────────────────────────────
print("whole-lot ledger")
check("target: exact half rounds DOWN (book the extra lot)", F.target(3, 1, 2) == 1 and F.target(5, 1, 2) == 2)
check("target: never below 1 while the owner holds", F.target(1, 1, 6) == 1 and F.target(2, 1, 6) == 1)
check("target: 0 once the owner is flat", F.target(3, 0, 2) == 0)
check("target: proportional adds", F.target(3, 12, 6) == 6 and F.target(2, 6, 4) == 3)

# ── 2. Rahul's real day, through the channel and DMs ──────────────────────────
print("real day: 26 Nov trades replayed on Fri 27 Nov")
tg, pub = reset("real")
at(27, 8, 0); pub.run_once(CLOCK[0])                             # service already running
subscribe("111", "small", "Asha"); subscribe("222", "standard", "Ravi")
at(27, 9, 15)
feed.scan_result(NS(action="CALL_SELL", strength="MODERATE", score=2, pcr=0.84, resistance_level=25966,
                    support_level=25680, call_wall=26000, put_wall=25500), 25917)
pub.run_once(CLOCK[0])
check("09:15 first scan → channel only", len(msgs(tg, CH)) == 1 and "First scan 09:15" in msgs(tg, CH)[0]
      and not msgs(tg, "111"), msgs(tg, CH))

at(27, 10, 30); t1 = call_sell(2, prem=167.80, hedge=27100, hprem=5.30, sl=26020)
feed.trade_entry(t1, 25917, strength="MODERATE"); pub.run_once(CLOCK[0])
ch = msgs(tg, CH)[-1]
check("channel entry: moderate → 2 lots (owner filled 2 of 4)", "Sell <b>2 lots</b> 26000 CE @ ₹167.80" in ch
      and "Hedge: buy <b>2 lots</b> 27100 CE" in ch and "above 26,020" in ch, ch)
btn = [m[3] for m in tg.sent if m[1] == CH][-1]
check("every channel post carries the bot button (deep link)", all(
      m[3] and m[3]["inline_keyboard"][0][0]["url"] == "https://t.me/ThetaDeskBot?start=channel"
      for m in tg.sent if m[1] == CH), btn)
check("DMs don't carry it", all(m[3] is None for m in tg.sent if m[1] != CH))
txt, _ = T.reply_for("/start channel", "777", "Kiran", {"channel": CH})
check("deep link '/start channel' gets the welcome", "Welcome, Kiran" in txt)
check("small DM: 1 lot", "Sell <b>1 lot</b> 26000 CE" in msgs(tg, "111")[-1], msgs(tg, "111")[-1])
check("standard DM: 2 lots", "Sell <b>2 lots</b> 26000 CE" in msgs(tg, "222")[-1])

at(27, 12, 0); feed.trade_exit(t1, 172.80, -812.0, "PROFIT_LOCK", 25935); pub.run_once(CLOCK[0])
ch = msgs(tg, CH)[-1]
check("trade 1 closed: buy back 2 lots, −₹812 at 2 lots", "TRADE CLOSED" in ch and "Buy back <b>2 lots</b>" in ch
      and "Trade result: <b>−₹812</b> (2 lots)" in ch, ch)
check("small DM: −₹406 at 1 lot", "Trade result: <b>−₹406</b> (1 lot)" in msgs(tg, "111")[-1], msgs(tg, "111")[-1])

at(27, 12, 15); t2 = call_sell(2)
feed.trade_entry(t2, 25945, strength="STRONG"); pub.run_once(CLOCK[0])
check("trade 2: strong → 3 lots on the channel (owner filled 2 of 6)", "Sell <b>3 lots</b> 26000 CE @ ₹180.00" in msgs(tg, CH)[-1]
      and "strong signal" in msgs(tg, CH)[-1], msgs(tg, CH)[-1])

at(27, 13, 15); feed.mark(6000.0, 25850); feed.pnl_snapshot(-812.0, t2); pub.run_once(CLOCK[0])
ch = msgs(tg, CH)[-1]
check("hourly: −₹812 booked + open 6000/2×3 = +₹9,000 → +₹8,188", "Day P&L: <b>+₹8,188</b>" in ch
      and "Open +₹9,000" in ch, ch)

at(27, 14, 0); feed.trade_exit(replace(t2, lots=1 * LOT), 96.20, 5346.0, "PROFIT_BOOK", 25780); pub.run_once(CLOCK[0])
ch, small = msgs(tg, CH)[-1], msgs(tg, "111")[-1]
check("owner books 1 of 2: channel books 2 of 3 (the extra lot), +₹10,692", "Buy back <b>2 lots</b>" in ch
      and "Booked: +₹10,692" in ch and "<b>1 lot</b> still open" in ch, ch)
check("small follower holds its single lot", "No action at your size — keep your 1 lot" in small, small)

at(27, 14, 45); t2.lots = 1 * LOT; feed.trade_exit(t2, 151.25, 1732.0, "PROFIT_LOCK", 25908); pub.run_once(CLOCK[0])
ch, small = msgs(tg, CH)[-1], msgs(tg, "111")[-1]
check("trade 2 result at 3 lots: 2×5,346 + 1×1,732 = +₹12,424", "Trade result: <b>+₹12,424</b> (3 lots)" in ch, ch)
check("small: closes its lot at the last exit, +₹1,732", "Buy back <b>1 lot</b>" in small and "+₹1,732</b> (1 lot)" in small, small)

at(27, 15, 35); feed.pnl_snapshot(6266.0, None, label="CLOSE"); pub.run_once(CLOCK[0])
ch = msgs(tg, CH)[-1]
check("channel close: +₹11,612, 2 trades", "Day P&L: <b>+₹11,612</b>" in ch and "Trades: 2 · ✅ 1 · ❌ 1" in ch
      and "Best +₹12,424 · Worst −₹812" in ch, ch)
check("personal close (small): −406 + 1,732 = +₹1,326", "Your day" in msgs(tg, "111")[-1]
      and "+₹1,326" in msgs(tg, "111")[-1], msgs(tg, "111")[-1])
check("owner's real figures never shown (₹6,266 day, ₹7,078 trade)",
      not any(x in m[2] for m in tg.sent for x in ("6,266", "7,078", "5,346</")))
if VERBOSE:
    print("\n" + "\n\n".join(f"[{a} → {c}]\n{t}" for a, c, t, _ in tg.sent) + "\n")

# ── 3. X shows the same rupees as the channel ─────────────────────────────────
print("X and Telegram agree")
X.OUTBOX, X.CURSOR, X.DRYRUN_LOG = T.OUTBOX, TMP / "real_xcursor.json", TMP / "real_xdry.log"
xsent = []
CLOCK[0] = datetime(2026, 11, 27, 8, 0, tzinfo=IST)
X.run_once({}, True, poster=lambda t, c: (xsent.append(t) or (True, False, "id")), now=CLOCK[0])
X.CURSOR.write_text(json.dumps({"seq": 0, "book": {}, "history": {}}))       # replay today's outbox into X
CLOCK[0] = datetime(2026, 11, 27, 15, 40, tzinfo=IST)
X.run_once({}, True, poster=lambda t, c: (xsent.append(t) or (True, False, "id")), now=CLOCK[0])
check("X trade 2 result = channel's +₹12,424", any("Trade result: +₹12,424 (3 lots)" in t for t in xsent), xsent)
check("X day close = channel's +₹11,612", any("Day P&L: +₹11,612" in t for t in xsent), [t[:60] for t in xsent])

# ── 4. the bot's buttons ──────────────────────────────────────────────────────
print("bot buttons")
tg, pub = reset("bot")
at(27, 8, 0); pub.run_once(CLOCK[0])
txt, kb = T.reply_for("hello", "333", "Meera", {"channel": CH})
check("anyone who messages is welcomed and offered sizes, Small first", "Welcome, Meera" in txt and "Pick your size" in txt
      and kb["inline_keyboard"][0][0]["callback_data"] == "plan:small"
      and kb["inline_keyboard"][1][0]["callback_data"] == "plan:standard", txt)
check("welcome: capital guidance, 1% aim, watch first, paid plan only when confident",
      "₹2 lakh → Small · 1 lot" in txt and "₹1.05–1.1 lakh" in txt and "₹5 lakh → Standard · 3/2/1" in txt
      and "about 1% of capital" in txt and "Follow the trades for a few weeks" in txt
      and "paid plan only once you're confident" in txt, txt)
J = TMP / "journal.csv"
J.write_text("lots,net_pnl\n" + "".join(f"65,{p}\n" for p in [1500] * 18 + [-800, -2841, -1000]))
T._stats_cache.clear()
s = T.risk_stats(J)
check("risk stats from the journal, per lot", s == {"trades": 21, "avg_loss": (-800 - 2841 - 1000) / 3, "worst": -2841, "over_2000": 1}, s)
T._paths.JOURNAL_FILE = J; T._stats_cache.clear()
txt2 = T.welcome_text("Meera", CH)
check("welcome shows the real record next to the 1% aim", "over 21 trades" in txt2 and "₹1,547 per lot on average" in txt2
      and "the worst cost ₹2,841" in txt2, txt2)
J.write_text("lots,net_pnl\n65,-500\n"); T._stats_cache.clear()
check("too few trades: no record line rather than a misleading one", "Our record" not in T.welcome_text("M", CH))
check("…and starts on Standard with alerts on", T.load_subs()["333"]["plan"] == "standard" and T.load_subs()["333"]["alerts"])
check("size button → Small", "Small · 1 lot" in T.on_callback("plan:small", "333") and T.load_subs()["333"]["plan"] == "small")
txt, _ = T.reply_for("📍 Trade & SL", "333", "Meera", {})
check("no open trade: says when trading happens", "No open trade" in txt and "10:00" in txt, txt)
at(27, 10, 0); t = call_sell(4, prem=150.0); feed.trade_entry(t, 25900, strength="STRONG"); pub.run_once(CLOCK[0])
txt, _ = T.reply_for("📍 Trade & SL", "333", "Meera", {})
check("open trade at your size + stop", "Short <b>1 lot</b> 26000 CE" in txt and "Stop: exit if Nifty spot goes above 26,050" in txt, txt)


class FakeKite:
    def __init__(self):
        self.orders = None

    def basket_order_margins(self, orders, consider_positions=False, mode=None):
        self.orders = orders
        return {"final": {"total": 31234.5 * orders[0]["quantity"] / 65}}


fk = FakeKite()
T._kite = lambda: fk
T._margin_cache.clear()
txt, _ = T.reply_for("💰 Margin", "333", "Meera", {})
check("margin: Zerodha's figure for your lots, hedge bought first", "₹31,234" in txt and "1 lot" in txt
      and [o["transaction_type"] for o in fk.orders] == ["BUY", "SELL"] and fk.orders[1]["quantity"] == 65, txt)
T.on_callback("plan:standard", "333"); T._margin_cache.clear()
txt, _ = T.reply_for("💰 Margin", "333", "Meera", {})
check("standard plan: 3 lots (strong)", "3 lots" in txt and "₹93,704" in txt and fk.orders[1]["quantity"] == 195, txt)
T._kite = lambda: (_ for _ in ()).throw(RuntimeError("today's Zerodha login hasn't happened yet"))
T._margin_cache.clear()
txt, _ = T.reply_for("💰 Margin", "333", "Meera", {})
check("margin unavailable: says so, no crash", "Couldn't get live margin" in txt, txt)
txt, _ = T.reply_for("🧾 Charges", "333", "Meera", {})
c = T.charges_for(F.new_position({"legs": [{"leg": "CE", "strike": 26000, "price": 150.0}], "entry_lots": 4,
                                  "strength": "STRONG", "structure": "CALL_SELL",
                                  "hedge": {"strike": 27050, "symbol": "H", "price": 5.0}}), 3)
check("charges: 4 orders, ₹80 brokerage, STT on sells only", c["orders"] == 4 and c["brokerage"] == 80
      and abs(c["stt"] - (150 + 5) * 195 * 0.001) < 1e-6, c)
check("charges reply shows the total as an estimate", f"Total ≈ ₹{c['total']:,.0f}" in txt and "Estimate" in txt, txt)
txt, _ = T.reply_for("🔔 Alerts", "333", "Meera", {})
check("alerts toggle off", "off" in txt and T.load_subs()["333"]["alerts"] is False)
at(27, 11, 0); feed.trade_exit(t, 100.0, 13000.0, "TARGET", 25800); pub.run_once(CLOCK[0])
check("alerts off → no DM", not msgs(tg, "333")[-1:] or "TRADE CLOSED" not in (msgs(tg, "333") or [""])[-1])
txt, _ = T.reply_for("📈 P&L", "333", "Meera", {})
check("P&L at your size: 13000/(4×65) × 65 × 3 = +₹9,750", "Today: <b>+₹9,750</b>" in txt, txt)

# ── 5. delivery rules ─────────────────────────────────────────────────────────
print("delivery")
tg, pub = reset("deliv")
at(27, 8, 0); pub.run_once(CLOCK[0])
subscribe("444", "small"); subscribe("555", "small")
tg.fail_next = {CH: [429], "555": [403]}
at(27, 10, 0); feed.trade_entry(call_sell(2), 25900, strength="MODERATE")
pub.run_once(CLOCK[0])
check("channel 429: DM still goes out at once, channel post stays queued",
      len(msgs(tg, "444")) == 1 and not msgs(tg, CH) and len(json.loads(T.STATE.read_text())["channel_queue"]) == 1)
pub.run_once(CLOCK[0])
check("retried: channel once, DM not repeated", len(msgs(tg, CH)) == 1 and len(msgs(tg, "444")) == 1)
check("user who blocked the bot (403) is switched off", T.load_subs()["555"].get("blocked") is True)
check("safe_html escapes P&L but keeps tags and entities",
      T.safe_html("<b>P&L</b> &amp; &lt;") == "<b>P&amp;L</b> &amp; &lt;")
check("every message would parse: no bare '&'",
      all("&" not in T.safe_html(m[2]).replace("&amp;", "").replace("&lt;", "").replace("&gt;", "") for m in tg.sent))

tg, pub = reset("dry", live=False)
at(27, 8, 0); pub.run_once(CLOCK[0]); subscribe("666", "small")
at(27, 10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); pub.run_once(CLOCK[0])
check("dry run: nothing sent, channel + DM text in the log", tg.sent == []
      and "Sell <b>3 lots</b>" in T.DRYRUN_LOG.read_text() and "Sell <b>1 lot</b>" in T.DRYRUN_LOG.read_text())

tg, pub = reset("hol")
CLOCK[0] = datetime(2026, 12, 25, 8, 0, tzinfo=IST); pub.run_once(CLOCK[0])
CLOCK[0] = datetime(2026, 12, 25, 10, 0, tzinfo=IST); feed.trade_entry(call_sell(2), 25900); pub.run_once(CLOCK[0])
check("NSE holiday: nothing posted", tg.sent == [])

tg, pub = reset("first")
at(27, 10, 0); feed.trade_entry(call_sell(2), 25900)
pub.run_once(CLOCK[0])
check("first start replays nothing", tg.sent == [] and json.loads(T.STATE.read_text())["seq"] == 1)

# ── 6. access: free month, then paid ─────────────────────────────────────────
print("access rules")
from publish import access as A
now = datetime(2026, 12, 1, 10, 0, tzinfo=IST)
sub = {"joined": now.isoformat()}
A.ensure_trial(sub, now)
check("trial = 30 days from joining", A.access_until(sub) == datetime(2026, 12, 31, 10, 0, tzinfo=IST)
      and A.status(sub, now) == "trial" and A.days_left(sub, now) == 30)
later = datetime(2026, 12, 31, 10, 0, tzinfo=IST)
check("expired at the end", A.status(sub, later) == "expired" and A.days_left(sub, later) == 0)
check("owner never expires", A.status(sub, datetime(2030, 1, 1, tzinfo=IST), is_owner=True) == "owner")
A.add_paid(sub, datetime(2026, 12, 20, tzinfo=IST), 1, 999, "owner")
check("paying during the trial adds a month after the trial end", A.access_until(sub) == datetime(2027, 1, 30, 10, 0, tzinfo=IST)
      and A.status(sub, datetime(2027, 1, 5, tzinfo=IST)) == "paid", A.access_until(sub))
A.add_paid(sub, datetime(2027, 3, 1, tzinfo=IST), 2, 1998, "owner")
check("paying after expiry starts from the payment day", A.access_until(sub) == datetime(2027, 4, 30, tzinfo=IST))
check("payments recorded", [p["amount"] for p in sub["payments"]] == [999, 1998])
A.revoke(sub, datetime(2027, 3, 5, tzinfo=IST))
check("revoke ends access now", A.status(sub, datetime(2027, 3, 5, 0, 1, tzinfo=IST)) == "expired")
subs_ = {"1": {"name": "Asha", "username": "asha_k"}, "2": {"name": "Ashok"}, "3": {"name": "Ravi"}}
check("find: id, @username, name prefix, ambiguity",
      A.find(subs_, "3") == ["3"] and A.find(subs_, "@asha_k") == ["1"] and A.find(subs_, "rav") == ["3"]
      and sorted(A.find(subs_, "ash")) == ["1", "2"] and A.find(subs_, "zz") == [])
n = {"joined": datetime(2026, 12, 1, 10, 0, tzinfo=IST).isoformat()}
A.ensure_trial(n, now)
seq = [(datetime(2026, 12, 25, 10, 0, tzinfo=IST), None), (datetime(2026, 12, 26, 11, 0, tzinfo=IST), 5),
       (datetime(2026, 12, 26, 15, 0, tzinfo=IST), None), (datetime(2026, 12, 30, 23, 0, tzinfo=IST), None),
       (datetime(2026, 12, 31, 9, 30, tzinfo=IST), 1), (datetime(2026, 12, 31, 12, 0, tzinfo=IST), 0),
       (datetime(2027, 1, 2, 10, 0, tzinfo=IST), None)]
got = [A.due_notice(n, t) for t, _ in seq]
check("reminders: 5 days, never at night, 1 day, then the expiry notice once",
      got == [e for _, e in seq], got)

print("bot access flow")
tg, pub = reset("access")
CLOCK[0] = datetime(2026, 12, 1, 9, 0, tzinfo=IST); pub.run_once(CLOCK[0])
side = []
send = lambda cid, text, markup=None: side.append((cid, text, markup))
ST = {"channel": CH, "owner_id": OWNER, "delay_min": 15}
txt, kb = T.reply_for("/start", "501", "Asha", ST, "asha_k", send)
check("new user: welcome + free access line", "Free access until <b>31 Dec 2026</b> (30 days left)" in txt, txt[-120:])
T.reply_for("/start", OWNER, "Rahul", ST, "", send)
CLOCK[0] = datetime(2026, 12, 1, 10, 0, tzinfo=IST)
feed.trade_entry(call_sell(2), 25900, strength="STRONG"); pub.run_once(CLOCK[0])
check("in trial: live DM", any("NEW TRADE" in m for m in msgs(tg, "501")))
CLOCK[0] = datetime(2026, 12, 26, 11, 0, tzinfo=IST); pub.run_once(CLOCK[0])
check("day 25: 5-day reminder with a pay button", any("5 days left" in m[2] and m[3] == T.pay_button()
                                                     for m in tg.sent if m[1] == "501"))
check("owner gets no reminders", not any("left" in m for m in msgs(tg, OWNER)))
CLOCK[0] = datetime(2027, 1, 1, 10, 0, tzinfo=IST); pub.run_once(CLOCK[0])
check("expiry notice sent", any("Live trade alerts are paused" in m for m in msgs(tg, "501")))
before = len(msgs(tg, "501"))
feed.trade_exit(call_sell(2), 50.0, 3000.0, "TARGET", 25800); pub.run_once(CLOCK[0])
check("expired: no live DMs", len(msgs(tg, "501")) == before, msgs(tg, "501")[-1:])
check("owner still gets them", any("TRADE CLOSED" in m for m in msgs(tg, OWNER)))
txt, kb = T.reply_for("💰 Margin", "501", "Asha", ST, "asha_k", send)
check("expired: buttons answer with how to continue", "Live trade alerts are paused" in txt and kb == T.pay_button(), txt)
ans = T.on_callback("pay:request", "501", ST, send)
check("'I want to continue' → owner is told who, with ready commands", side and side[-1][0] == OWNER
      and "Asha" in side[-1][1] and "@asha_k" in side[-1][1] and "/paid 501 1" in side[-1][1] and "We've got your request" in ans, side[-1:])
txt, _ = T.reply_for("/paid 501 1 999", "501", "Asha", ST, "asha_k", send)
check("non-owner can't run /paid", "Live trade alerts are paused" in txt)
txt, _ = T.reply_for("/paid @asha_k 1 999", OWNER, "Rahul", ST, "", send)
check("owner /paid: recorded, member told", "Asha: paid 1 month (₹999)" in txt and "31 Jan 2027" in txt
      and side[-1][0] == "501" and "Payment received" in side[-1][1], txt)
check("paid member gets live DMs again", T.reply_for("📍 Trade & SL", "501", "Asha", ST)[0].startswith("No open trade")
      or "Open trade" in T.reply_for("📍 Trade & SL", "501", "Asha", ST)[0])
txt, _ = T.reply_for("/members", OWNER, "Rahul", ST, "", send)
check("/members lists status, end date and amount paid", "<code>501</code> Asha @asha_k" in txt and "<b>paid</b>" in txt
      and "paid ₹999" in txt and "<b>owner</b>" in txt, txt)
txt, _ = T.reply_for("/msg ash Your UPI ID is abc@upi", OWNER, "Rahul", ST, "", send)
check("/msg relays through the bot", side[-1][0] == "501" and "abc@upi" in side[-1][1] and "Sent to Asha" in txt)
txt, _ = T.reply_for("/revoke 501", OWNER, "Rahul", ST, "", send)
check("/revoke ends access", "access ended now" in txt and not A.has_access(T.load_subs()["501"], CLOCK[0]))
txt, _ = T.reply_for("/extend 501 7", OWNER, "Rahul", ST, "", send)
check("/extend gives days back", "+7 days" in txt and A.status(T.load_subs()["501"], CLOCK[0]) == "paid", txt)

print("channel runs 15 min late")
tg, pub = reset("delay", delay=15)
CLOCK[0] = datetime(2026, 12, 2, 8, 0, tzinfo=IST); pub.run_once(CLOCK[0])
T.reply_for("/start", "601", "Dev", {"channel": CH, "owner_id": OWNER})
CLOCK[0] = datetime(2026, 12, 2, 9, 15, tzinfo=IST)
feed.scan_result(NS(action="PUT_SELL", strength="MODERATE", score=2, pcr=1.1, resistance_level=26000,
                    support_level=25700, call_wall=26000, put_wall=25600), 25800)
pub.run_once(CLOCK[0])
check("09:15 scan not in the channel yet", not msgs(tg, CH))
CLOCK[0] = datetime(2026, 12, 2, 10, 0, tzinfo=IST)
t = call_sell(2); feed.trade_entry(t, 25900, strength="STRONG"); pub.run_once(CLOCK[0])
check("10:00: the DM is live, the channel only has the 09:15 scan (posted at 09:30+)",
      any("NEW TRADE" in m for m in msgs(tg, "601")) and len(msgs(tg, CH)) == 1 and "First scan" in msgs(tg, CH)[0])
CLOCK[0] = datetime(2026, 12, 2, 10, 5, tzinfo=IST); feed.trade_exit(t, 90.0, 5850.0, "TARGET", 25850); pub.run_once(CLOCK[0])
check("10:05: exit DM live, channel still waiting", any("TRADE CLOSED" in m for m in msgs(tg, "601")) and len(msgs(tg, CH)) == 1)
CLOCK[0] = datetime(2026, 12, 2, 10, 15, tzinfo=IST); pub.run_once(CLOCK[0])
check("10:15: entry reaches the channel, exit not yet", len(msgs(tg, CH)) == 2 and "NEW TRADE" in msgs(tg, CH)[1]
      and "🕒 Posted 15 min after the trade" in msgs(tg, CH)[1] and "⏰ 10:00 IST" in msgs(tg, CH)[1])
CLOCK[0] = datetime(2026, 12, 2, 10, 20, tzinfo=IST); pub.run_once(CLOCK[0])
check("10:20: exit follows, order kept", len(msgs(tg, CH)) == 3 and "TRADE CLOSED" in msgs(tg, CH)[2])
tg.fail_next = {CH: [429]}
CLOCK[0] = datetime(2026, 12, 2, 15, 35, tzinfo=IST); feed.pnl_snapshot(5850.0, None, label="CLOSE"); pub.run_once(CLOCK[0])
CLOCK[0] = datetime(2026, 12, 2, 15, 50, tzinfo=IST); pub.run_once(CLOCK[0])
check("channel 429 keeps the post queued", len(msgs(tg, CH)) == 3)
pub.run_once(CLOCK[0])
check("…and sends it on the next pass", len(msgs(tg, CH)) == 4 and "Day closed" in msgs(tg, CH)[3])
check("queue survives a restart (kept in state)", json.loads(T.STATE.read_text())["channel_queue"] == [])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
