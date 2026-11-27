"""
Offline tests for the end-of-day card (publish/day_card.py). Nothing is posted: X, Telegram and Zerodha
margins are faked. Rahul's real 26 Nov day is replayed through the real pipeline.
Run from the repo root:   python3 tests/test_day_card.py
"""
import io, json, os, sys, tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, day_card as DC, x_poster as X
from signals.trade_monitor import TradeState

TMP = Path(tempfile.mkdtemp())
IST = X.IST
LOT = 65
CLOCK = [datetime(2026, 11, 26, 9, 0, tzinfo=IST)]
feed._now = lambda: CLOCK[0]
feed.OUTBOX = DC.OUTBOX = TMP / "outbox.jsonl"
feed.STATE = TMP / "feed.json"
DC.STATE = TMP / "day_card.json"
X.HOLIDAYS_FILE = TMP / "hol.txt"; X.HOLIDAYS_FILE.write_text("2026-12-25\n")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def at(h, m):
    CLOCK[0] = datetime(2026, 11, 26, h, m, tzinfo=IST)


def cs(lots, strike=26000, prem=180.0, hedge=27050, hprem=5.0, sl=26050):
    return TradeState(action="CALL_SELL", strike=strike, symbol=f"NIFTY26D01{strike}CE", entry_time=CLOCK[0], entry_premium=prem,
                      entry_spot=25945, sl_spot_level=sl, expiry="01-Dec-2026", lots=lots * LOT,
                      hedge_symbol=f"NIFTY26D01{hedge}CE", hedge_strike=hedge, hedge_entry_premium=hprem)


at(10, 30); t1 = cs(2, prem=167.80, hedge=27100, hprem=5.30, sl=26020); feed.trade_entry(t1, 25917, strength="MODERATE")
at(11, 15); feed.mark(-400.0, 25930); feed.pnl_snapshot(0.0, t1)
at(12, 0); feed.trade_exit(t1, 172.80, -812.0, "PROFIT_LOCK", 25935)
at(12, 15); t2 = cs(2); feed.trade_entry(t2, 25945, strength="STRONG")
at(13, 15); feed.mark(6000.0, 25850); feed.pnl_snapshot(-812.0, t2)
at(14, 0); feed.trade_exit(replace(t2, lots=LOT), 96.20, 5346.0, "PROFIT_BOOK", 25780)
at(14, 45); t2.lots = LOT; feed.trade_exit(t2, 151.25, 1732.0, "PROFIT_LOCK", 25908)
at(15, 35); feed.pnl_snapshot(6266.0, None, label="CLOSE")

margins = {2: 160_000.0, 3: 240_000.0}
d = DC.build(DC.day_events("2026-11-26"), lambda pos, lots: margins[lots])
check("day P&L at plan size = −812 + 12,424 = +11,612", d["pnl"] == 11612, d["pnl"])
check("lots per trade: moderate 2, strong 3", [t["lots"] for t in d["trades"]] == [2, 3])
check("entry prices", [round(t["legs"][0]["entry"], 2) for t in d["trades"]] == [167.80, 180.00])
check("exit price = follower's fills: 2 lots @ 96.20 + 1 @ 151.25 → 114.55", round(d["trades"][1]["legs"][0]["exit"], 2) == 114.55,
      d["trades"][1]["legs"][0]["exit"])
check("margin utilised = the biggest position's margin (3 lots)", d["margin_peak"] == 240_000)
check("return on margin and on ₹5L capital", round(d["ret_margin"], 4) == round(11612 / 240000, 4) and d["ret_capital"] == 11612 / 500000)
check("MTM: close +11,612, low −400 (11:15 mark), high +11,612", d["mtm_close"] == 11612 and d["mtm_low"] == -400
      and d["mtm_high"] == 11612, (d["mtm_low"], d["mtm_high"]))
check("flat at close → ₹0 blocked", d["blocked_at_close"] == 0)
check("Indian digit grouping", DC.indian(241649) == "2,41,649" and DC.indian(500000) == "5,00,000" and DC.indian(1234567) == "12,34,567"
      and DC.indian(999) == "999" and DC._rs(-11612) == "−₹11,612")
png = DC.render(d, date(2026, 11, 26))
from PIL import Image
im = Image.open(io.BytesIO(png))
check("renders a 1200×675 PNG", im.size == (1200, 675) and png[:4] == b"\x89PNG")
cap = DC.caption(d, date(2026, 11, 26))
check("caption: P&L, returns, hashtags, no links, fits X", "+₹11,612" in cap and "Return on margin +4.84%" in cap
      and cap.endswith(DC.HASHTAGS) and "http" not in cap and X._weight(cap) <= 280, cap)
d0 = DC.build(DC.day_events("2026-11-26"), lambda pos, lots: (_ for _ in ()).throw(RuntimeError("no token")))
check("Zerodha margin unavailable → card still built, margin shown as —", d0["margin_peak"] is None and d0["ret_margin"] is None
      and DC._inr(None) == "—")

print("posting")
calls = []
xp = lambda png, text, creds: calls.append(("x", text)) or "X1"
tp = lambda png, text: calls.append(("tg", text)) or 77
X.ENV_FILE = TMP / "x.env"; X.ENV_FILE.write_text("X_API_KEY=a\nX_API_SECRET=b\nX_ACCESS_TOKEN=c\nX_ACCESS_SECRET=d\nX_LIVE=1\n")
out = DC.run(date(2026, 11, 26), True, margin_of=lambda p, l: margins[l], x_post=xp, tg_post=tp)
check("posts on X and Telegram once", out.get("x") == "X1" and out.get("tg") == 77 and [c[0] for c in calls] == ["x", "tg"], out)
DC.run(date(2026, 11, 26), True, margin_of=lambda p, l: margins[l], x_post=xp, tg_post=tp)
check("running again the same day posts nothing", len(calls) == 2)
out = DC.run(date(2026, 11, 27), True, margin_of=lambda p, l: 1.0, x_post=xp, tg_post=tp)
check("a day without trades: skipped", out.get("skipped") == "no trade today" and len(calls) == 2)
X.ENV_FILE.write_text("X_API_KEY=a\nX_API_SECRET=b\nX_ACCESS_TOKEN=c\nX_ACCESS_SECRET=d\nX_LIVE=0\n")
DC.STATE.unlink()
out = DC.run(date(2026, 11, 26), True, margin_of=lambda p, l: 1.0, x_post=xp, tg_post=tp)
check("X dry run → no X post (Telegram still posts)", "x" not in out and out.get("tg") == 77)
DC.STATE.unlink()
bad_tg = lambda png, text: (_ for _ in ()).throw(RuntimeError("Telegram is not live"))
out = DC.run(date(2026, 11, 26), True, margin_of=lambda p, l: 1.0, x_post=xp, tg_post=bad_tg)
check("a failing channel is reported, not fatal", "Telegram is not live" in out.get("tg_error", ""))


class FakeHTTP:
    def __init__(self): self.seen = []
    def post(self, url, **kw):
        self.seen.append((url, kw))
        class R:
            status_code = 200
            def __init__(s, b): s.b = b; s.text = json.dumps(b)
            def json(s): return s.b
        return R({"data": {"id": "M9"}}) if "media/upload" in url else R({"data": {"id": "T5"}})


h = FakeHTTP()
tid = DC.post_x(b"\x89PNG..", "hi #Nifty", {"api_key": "k", "api_secret": "s", "access_token": "t", "access_secret": "u"}, http=h)
check("X: upload the image, then a post carrying its media id", tid == "T5" and "media/upload" in h.seen[0][0]
      and h.seen[1][1]["json"] == {"text": "hi #Nifty", "media": {"media_ids": ["M9"]}})

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
