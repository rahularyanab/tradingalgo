"""Offline tests for scripts/go_live.py (the unattended verify -> wait for funds -> start sequence).  python3 tests/test_go_live.py"""
import importlib.util
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "tests"))
spec = importlib.util.spec_from_file_location("go_live", ROOT / "scripts" / "go_live.py")
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)
import verify_angel as V                                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


GOOD = [(V.OK, "profile", ""), (V.OK, "option open interest", "50 of 50")]
BAD = [(V.OK, "profile", ""), (V.BAD, "order book", "missing fields"), (V.OK, "option open interest", "50")]


class Env:
    def __init__(self, t0, cash_by_poll, verify_results, start_ok=True):
        self.t = t0; self.msgs = []; self.cash = list(cash_by_poll); self.vr = list(verify_results)
        self.started = 0; self.recorded = 0; self.verified = []; self.start_ok = start_ok
        self.client = self

    def now(self): return self.t
    def sleep(self, s): self.t += timedelta(seconds=s)
    def rms(self):
        c = self.cash.pop(0) if len(self.cash) > 1 else self.cash[0]
        if c is None:
            raise RuntimeError("api down")
        return {"availablecash": f"{c:.2f}", "net": f"{c:.2f}"}
    def verify(self, c, test_order): self.verified.append(test_order); return self.vr.pop(0) if len(self.vr) > 1 else self.vr[0]
    def record(self, res): self.recorded += 1
    def start(self): self.started += 1; return (self.start_ok, "" if self.start_ok else "unit failed")

    def go(self, **kw):
        a = dict(say=self.msgs.append, verify=self.verify, record=self.record, start=self.start, min_funds=110_000,
                 start_at=dtime(9, 20), give_up_at=dtime(13, 0), test_order=True, now=self.now, sleep=self.sleep)
        a.update(kw)
        return G.go_live(self, **a)


WED = datetime(2026, 10, 28, 5, 30)
print("[1] the happy path")
e = Env(WED, [78_000, 78_000, 120_000], [GOOD])
rc = e.go()
check("waits until 09:20, verifies WITH the test order, records the pass, waits for funds, starts the bot once", rc == 0 and e.started == 1 and e.recorded == 1 and e.verified == [True] and e.t.time() >= dtime(9, 20), str((rc, e.t)))
check("tells you each step on Telegram, incl. the waiting-for-funds notice and the LIVE message",
      any("Go-live check started" in m for m in e.msgs) and any("Waiting for funds" in m for m in e.msgs) and "LIVE" in e.msgs[-1], str(e.msgs))
check("the verification ran before the funds wait and before the start", e.msgs.index(next(m for m in e.msgs if "verification passed" in m)) < e.msgs.index(next(m for m in e.msgs if "Waiting for funds" in m)))
e = Env(WED.replace(hour=9, minute=40), [150_000], [GOOD]); rc = e.go(test_order=False)
check("funds already there: starts immediately, no test order when not requested", rc == 0 and e.verified == [False] and e.started == 1)
e = Env(WED.replace(hour=9, minute=40), [150_000], [GOOD]); e.go()
check("funds already there: no 'waiting' message at all", sum("Waiting for funds" in m for m in e.msgs) == 0)
e = Env(WED, [50_000], [GOOD]); e.go(give_up_at=dtime(11, 0))
w = [m for m in e.msgs if "Waiting for funds" in m]
check("while waiting 09:20-11:00 there is one notice per half-hour bucket (4), not one per 2-minute poll (50)", len(w) == 4, str(len(w)))

print("\n[2] it does NOT start when it should not")
e = Env(WED, [150_000], [BAD]); rc = e.go()
check("a failed verification (after one retry) → bot NOT started, nothing recorded, the failure is named", rc == 1 and e.started == 0 and e.recorded == 0 and len(e.verified) == 2 and "order book" in e.msgs[-1] and "NOT started" in e.msgs[-1], str(e.msgs[-1]))
e = Env(WED, [150_000], [BAD, GOOD]); rc = e.go()
check("a transient failure that passes on the retry still goes live", rc == 0 and e.started == 1 and len(e.verified) == 2)
e = Env(WED, [150_000], [[(V.OK, "profile", ""), (V.WARN, "market is closed", "")]]); rc = e.go()
check("an inconclusive run (e.g. no open interest) is a failure, not a pass", rc == 1 and e.started == 0)
e = Env(WED, [78_000], [GOOD]); rc = e.go()
check("funds never arrive → NOT started once 13:00 passes, and it says so", rc == 2 and e.started == 0 and "Funds did not arrive" in e.msgs[-1] and e.t.time() >= dtime(13, 0), str((rc, e.t)))
e = Env(WED, [None], [GOOD]); rc = e.go(give_up_at=dtime(10, 0))
check("an unreadable balance is never treated as 'enough'", rc == 2 and e.started == 0)
e = Env(datetime(2026, 10, 31, 8, 0), [150_000], [GOOD]); rc = e.go()
check("on a Saturday: nothing happens", rc == 3 and e.started == 0 and e.verified == [])
e = Env(WED.replace(hour=14, minute=0), [150_000], [GOOD]); rc = e.go()
check("launched after the give-up time: not started", rc == 2 and e.started == 0 and e.verified == [])
e = Env(WED, [150_000], [GOOD], start_ok=False); rc = e.go()
check("the service failing to start is reported, exit 1", rc == 1 and "Could not start" in e.msgs[-1])
e = Env(WED, [110_000], [GOOD]); rc = e.go()
check("exactly the minimum counts as enough", rc == 0)
e = Env(WED, [109_999.99], [GOOD]); rc = e.go(give_up_at=dtime(9, 30))
check("one paisa less does not", rc == 2)
e = Env(WED, [150_000], [[(V.OK, "profile", ""), (V.OK, "option open interest", "x"), (V.WARN, "order FILLED", "you now hold 65 of X @ 0.05")]]); rc = e.go()
check("if the test order unexpectedly FILLED you are told to square it off", any("FILLED" in m and "Square" in m for m in e.msgs))

e = Env(WED, [150_000], [GOOD]); rc = e.go(headroom=lambda: 150)
check("not enough memory headroom → the bot is NOT started and the reason is explained", rc == 1 and e.started == 0 and "short of memory" in e.msgs[-1], str(e.msgs[-1]))
e = Env(WED, [150_000], [GOOD]); rc = e.go(headroom=lambda: G.MIN_HEADROOM_MB)
check("exactly the required headroom is enough", rc == 0 and e.started == 1)
check("memory_headroom_mb reads this machine without error (or /proc is absent on macOS)", True if not Path("/proc/meminfo").exists() else G.memory_headroom_mb() > 0)

print("\n[3] helpers")
class C1:
    def rms(self): return {"availablecash": "78307.94", "net": "1"}
class C2:
    def rms(self): return {"net": "5000"}
class C3:
    def rms(self): return {"weird": "x"}
check("availablecash preferred", G.free_cash(C1()) == 78307.94)
check("falls back to net", G.free_cash(C2()) == 5000.0)
try:
    G.free_cash(C3()); err = False
except RuntimeError as ex:
    err = "weird" in str(ex)
check("unknown reply shape raises, naming the keys", err)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
