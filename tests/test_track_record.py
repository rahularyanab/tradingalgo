"""
Offline tests for the public track record (publish/track_record.py). Telegraph is faked; nothing is published.
Run from the repo root:   python3 tests/test_track_record.py
"""
import json, os, sys, tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.getcwd())
from publish import track_record as R

TMP = Path(tempfile.mkdtemp())
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


H = "date,exit_time,action,strike,expiry,entry_time,entry_spot,exit_spot,entry_premium,exit_premium,hedge_strike,hedge_entry_premium,hedge_exit_premium,lots,main_pnl,hedge_pnl,net_pnl,exit_reason\n"
rows = [  # Rahul's real 26 Nov day: trade 2 exited in two slices of 1 lot each
    "2026-11-26,12:00,CALL_SELL,26000,01-Dec-2026,10:30,25917,25935,167.80,172.80,27100,5.30,4.05,130,-650,-162,-812,PROFIT_LOCK",
    "2026-11-26,14:00,CALL_SELL,26000,01-Dec-2026,12:15,25945,25780,180.00,96.20,27050,5.00,3.45,65,5447,-101,5346,PROFIT_BOOK",
    "2026-11-26,14:45,CALL_SELL,26000,01-Dec-2026,12:15,25945,25908,180.00,151.25,27050,5.00,2.90,65,1869,-136,1732,PROFIT_LOCK",
    "2026-10-05,11:00,PUT_SELL,24500,07-Oct-2026,10:00,24600,24480,90,140,24000,4,6,195,-9750,390,-9360,SL_HIT",
    "2026-10-06,13:00,STRANGLE_CE,25000,07-Oct-2026,10:00,24600,24700,60,20,,,,130,5200,0,5200,EOD",
    "2026-10-06,13:00,STRANGLE_PE,24200,07-Oct-2026,10:00,24600,24700,50,10,,,,130,5200,0,5200,EOD",
]
J = TMP / "journal.csv"
J.write_text(H + "\n".join(sorted(rows)) + "\n")
R.JOURNAL = J
t = R.load_trades()
check("exit slices of one trade are merged (6 rows → 5 trades)", len(t) == 5, [x["per_lot"] for x in t])
two = next(x for x in t if x["entry"] == "12:15")
check("per lot = net ₹ (hedge incl.) ÷ lots held: (5346+1732)/2 = 3539", round(two["per_lot"]) == 3539 and two["lots"] == 2)
check("the loser is in too: −812/2 = −406", any(round(x["per_lot"]) == -406 for x in t))
s = R.stats(t)
check("stats: count, wins, worst, drawdown, months", s["n"] == 5 and s["wins"] == 3 and round(s["worst"]) == -3120
      and round(s["max_dd"]) == -3120 and list(s["months"]) == ["2026-10", "2026-11"], s)
text = R.as_text(R.nodes(t, datetime(2026, 11, 27, 16, 0, tzinfo=R.IST)))
check("page shows total, worst trade, worst drawdown and the disclaimers", "Total:" in text and "worst trade −₹3,120" in text
      and "Worst drawdown −₹3,120" in text and "Before brokerage" in text and "Not investment advice" in text, text)
check("page never shows ₹ at the owner's real size (₹7,078 / −₹9,360)", "7,078" not in text and "9,360" not in text)


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append((url.rsplit("/", 1)[-1] if "editPage" not in url else "editPage", data))
        class Resp:
            def __init__(s_, body): s_.body = body
            def json(s_): return s_.body
        m = url.split("/")[-1] if "editPage" not in url else "editPage"
        if m == "createAccount":
            return Resp({"ok": True, "result": {"access_token": "TOKEN123"}})
        return Resp({"ok": True, "result": {"path": "Theta-Desk-11-27", "url": "https://telegra.ph/Theta-Desk-11-27"}})


R.STATE = TMP / "telegraph.json"
http = FakeHTTP()
check("daily update before the first approved publish does nothing", R.publish(R.nodes(t, datetime.now(R.IST)), http, create=False) is None
      and http.calls == [])
url = R.publish(R.nodes(t, datetime.now(R.IST)), http)
check("first publish: account + page created, URL returned", url == "https://telegra.ph/Theta-Desk-11-27"
      and [c[0] for c in http.calls] == ["createAccount", "createPage"])
check("…token kept private (file mode 600)", oct(R.STATE.stat().st_mode)[-3:] == "600" and json.loads(R.STATE.read_text())["access_token"] == "TOKEN123")
R.publish(R.nodes(t, datetime.now(R.IST)), http, create=False)
check("later runs EDIT the same page (same link forever)", http.calls[-1][0] == "editPage" and len(http.calls) == 3)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
