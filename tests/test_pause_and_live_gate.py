"""
Offline tests for: Pause/Resume (control.py, the Telegram commands, the gates in main.py), the single-lot profile keys
(allow_pyramiding, owner), the live-Angel verification gate, the Angel position-sign cross-check, provisioning flags.

    python3 tests/test_pause_and_live_gate.py

Every pause file is redirected to a temp dir — this test never touches the real pause_all.flag, logs or S/R database.
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("ALGO_USER_DIR", None)

import paths                                                      # noqa: E402

TMP = Path(tempfile.mkdtemp())
paths.PAUSE_FILE = TMP / ".paused"
paths.GLOBAL_PAUSE_FILE = TMP / "pause_all.flag"
paths.STATE_FILE = TMP / "state.json"
paths.ANGEL_VERIFIED_FILE = TMP / ".angel_verified"

import control                                                    # noqa: E402
import broker                                                     # noqa: E402
import sizing                                                     # noqa: E402
import userprofile                                                # noqa: E402
from fakes_angel import (FakeAngel, make_client, sym, opt_price, BY_SYMBOL, EXP1, SPOT)   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def md_ok(text):
    if text.count("`") % 2:
        return False
    plain = re.sub(r"`[^`]*`", "", text)
    return plain.count("_") % 2 == 0 and plain.count("*") % 2 == 0


def clear():
    paths.PAUSE_FILE.unlink(missing_ok=True)
    paths.GLOBAL_PAUSE_FILE.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
print("\n[1] control.py — the two switches")
clear()
check("starts running", control.is_paused() == (False, ""))
check("pause() reports a change, then 'already'", control.pause() is True and control.pause() is False)
p, why = control.is_paused()
check("paused with a human reason", p and "paused by you" in why, why)
check("resume() clears it, and says so only once", control.resume() is True and control.resume() is False and not control.is_paused()[0])
control.pause_all()
p, why = control.is_paused()
check("pause_all pauses everyone and says it was the owner", p and "ALL bots" in why, why)
control.pause()
control.resume()
check("a user's /resume does NOT lift the owner's pause_all", control.is_paused()[0] and control.global_paused())
check("resume_all lifts it", control.resume_all() is True and not control.is_paused()[0])
control.pause(); control.pause_all(); control.resume_all()
check("resume_all leaves a user's own pause in place", control.is_paused()[0] and "by you" in control.is_paused()[1])
clear()
paths.PAUSE_FILE.write_text("not json {{{")
check("a corrupt pause file still means PAUSED (fail safe) with a readable reason", control.is_paused()[0] and "by you (earlier)" in control.is_paused()[1], str(control.is_paused()))
clear()
check("note with no state file is empty", control.open_trade_note() == "")
paths.STATE_FILE.write_text(json.dumps({"trade": None}))
check("note says no trade is open", control.open_trade_note() == "No trade is open.")
paths.STATE_FILE.write_text(json.dumps({"trade": {"action": "CALL_SELL", "strike": 23000, "lots": 65}}))
n = control.open_trade_note()
check("note describes the open trade in LOTS and says protection continues", "CALL SELL 23000 (1 lot)" in n and "stop-loss" in n, n)
paths.STATE_FILE.write_text(json.dumps({"trade": {"action": "STRANGLE", "strike": 23000, "lots": 130}}))
check("plural lots", "(2 lots)" in control.open_trade_note())

# ──────────────────────────────────────────────────────────────────────────────
print("\n[2] Telegram commands")
from notifications import telegram_commands as TC                 # noqa: E402

sent = []
TC._send = lambda chat, text, plain=False: sent.append(text)
owner = SimpleNamespace(owner=True)
member = SimpleNamespace(owner=False)


def say(cmd, settings):
    TC.SETTINGS = settings
    sent.clear()
    TC._handle(cmd, "1")
    return "\n".join(sent)


clear()
r = say("/pause", member)
check("/pause pauses and explains what keeps running", control.is_paused()[0] and "Paused" in r and "stop-loss" in r and "3:25" in r, r)
check("/pause twice says already paused", "Already paused" in say("/pause", member))
check("/status shows PAUSED", "PAUSED" in say("/status", member))
check("/resume resumes", "Resumed" in say("/resume", member) and not control.is_paused()[0])
check("/resume when not paused is harmless", "Not paused" in say("/resume", member))
check("/status shows running", "Running" in say("/status", member))
r = say("/pauseall", member)
check("a family member cannot /pauseall (nothing written)", "Only the owner" in r and not control.global_paused(), r)
r = say("/resumeall", member)
check("…nor /resumeall", "Only the owner" in r)
r = say("/pauseall", owner)
check("the owner can /pauseall", control.global_paused() and "ALL bots" in r, r)
r = say("/resume", member)
check("a member's /resume is told the owner's pause stays", "owner has paused ALL" in r and control.global_paused(), r)
check("owner /resumeall lifts it", "resumed" in say("/resumeall", owner) and not control.global_paused())
check("/resumeall with nothing active says so", "No server-wide pause" in say("/resumeall", owner))
check("a bot without a profile (the owner's own instance) may /pauseall", say("/pauseall", None) and control.global_paused())
clear()
check("@botname suffix still works", say("/pause@MyBot", member) and control.is_paused()[0])
clear()
cmds = {c["command"] for c in TC._BOT_COMMANDS}
check("all five commands are in Telegram's menu", {"pause", "resume", "status", "pauseall", "resumeall"} <= cmds)
h_member, h_owner = say("/help", member), say("/help", owner)
check("/help lists /pause for everyone but /pauseall only for the owner", "/pause" in h_member and "/pauseall" not in h_member and "/pauseall" in h_owner)
check("every reply is valid Telegram Markdown (an unbalanced _ or * silences the whole message)",
      all(md_ok(say(c, s)) for c in ("/pause", "/status", "/resume", "/help", "/pauseall", "/resumeall") for s in (member, owner)))
clear()
from notifications import onboarding                               # noqa: E402
st = userprofile.build(userprofile.parse(userprofile.render_template("Asha", "zerodha", 1000000)), {"MAX_START_LOTS": 3})
check("the welcome guide teaches /pause, /resume, /status and is valid Markdown",
      all(w in onboarding.welcome_messages(st)[2] for w in ("/pause", "/resume", "/status")) and all(md_ok(m) for m in onboarding.welcome_messages(st)))

# ──────────────────────────────────────────────────────────────────────────────
print("\n[3] main.py honours the pause — on the REAL entry paths — and never blocks protection")
import data.sr_database as _srdb                                   # noqa: E402
_srdb._DB_FILE = TMP / "sr_levels.json"
import main                                                        # noqa: E402
from data import option_chain as ocmod                             # noqa: E402
from signals.combiner import FinalSignal                           # noqa: E402
from signals.trade_monitor import TradeState                       # noqa: E402
from signals.position_manager import ManagementDecision, REVERSE_PUT_SELL, PYRAMID_ADD   # noqa: E402

main._STATE_FILE = TMP / "main_state.json"
main._post = lambda t: True
main.send_error_alert = lambda t: None
main.send_signal = lambda *a, **k: None
main._is_market_open = lambda: True
main._entry_allowed = lambda: True
sell_calls = lambda c: [o for o in c.orders() if o["transaction_type"] == "SELL"]
buy_calls = lambda c: [o for o in c.orders() if o["transaction_type"] == "BUY"]

fk = FakeAngel()
c, _, _ = make_client(fk, paper=True)
main._get_kite = lambda: c
k = round(SPOT / 50) * 50 + 100
put_sym = sym(EXP1, k - 300, "PE")
call_sym = sym(EXP1, k, "CE")
put_px = opt_price(BY_SYMBOL[put_sym], SPOT)
new_sig = FinalSignal(action="PUT_SELL", strength="STRONG", score=3, lots=1, strike=k - 300, symbol=put_sym, premium=put_px,
                      sl_spot_level=SPOT - 60, expiry=EXP1.strftime("%d-%b-%Y"), spot_price=SPOT)
main.combine_signals = lambda **kw: new_sig
oc = SimpleNamespace(weekly_expiry_date=EXP1.strftime("%d-%b-%Y"), call_data={}, put_data={})


def open_call():
    main.monitor.clear_trade(); main.strangle_legs = None
    t = TradeState(action="CALL_SELL", strike=k, symbol=call_sym, entry_time=datetime.now(), entry_premium=80.0, entry_spot=SPOT,
                   sl_spot_level=SPOT + 60, expiry=EXP1.strftime("%d-%b-%Y"), lots=65)
    main.monitor.set_trade(t)
    return t


rev = ManagementDecision(action=REVERSE_PUT_SELL, reason="test reversal", score=3, new_action="PUT_SELL")

clear()
t = open_call()
main._handle_management_decision(rev, t, SPOT, None, None, None, oc, None)
check("CONTROL (not paused): a reversal closes the old trade AND enters the new one",
      main.monitor.trade is not None and main.monitor.trade.symbol == put_sym and len(sell_calls(c)) == 1, str(main.monitor.trade))

fk2 = FakeAngel(); c, _, _ = make_client(fk2, paper=True); main._get_kite = lambda: c
control.pause()
t = open_call()
main._handle_management_decision(rev, t, SPOT, None, None, None, oc, None)
check("PAUSED: a reversal still CLOSES the old position (protection is never blocked)…", len(buy_calls(c)) >= 1 and buy_calls(c)[0]["tradingsymbol"] == call_sym, str(c.orders()))
check("…but does NOT open the new one: no sell order, nothing tracked", main.monitor.trade is None and sell_calls(c) == [], str(sell_calls(c)))

# a pyramid add must not touch the broker while paused
class Spy:
    def __init__(self): self.touched = []
    def __getattr__(self, n): self.touched.append(n); raise AttributeError(n)


add = ManagementDecision(action=PYRAMID_ADD, reason="test add", score=3, lots=1)
t = open_call()
spy = Spy()
try:
    main._handle_management_decision(add, t, SPOT, None, None, None, spy, None)
except Exception as e:
    spy.touched.append(f"raised {e!r}")
check("PAUSED: a pyramid add returns before doing anything (no broker or chain access)", spy.touched == [], str(spy.touched))
control.resume()
spy = Spy()
try:
    main._handle_management_decision(add, t, SPOT, None, None, None, spy, None)
except Exception:
    pass
check("CONTROL (not paused): the same add proceeds into its work", spy.touched != [], str(spy.touched))
main.monitor.clear_trade()

# the main entry branch inside run_scan, with a forced signal
import random                                                       # noqa: E402
random.seed(4)
walk, px = [], SPOT - 150
base = datetime(2026, 10, 19, 9, 15)
for i in range(150):
    o = px; px += random.uniform(-14, 15); h = max(o, px) + random.uniform(0, 8); l = min(o, px) - random.uniform(0, 8)
    day, slot = divmod(i, 25)
    walk.append([(base + timedelta(days=day, minutes=15 * slot)).strftime("%Y-%m-%dT%H:%M:%S+0530"), o, h, l, px, 0])
from fakes_angel import Resp                                        # noqa: E402
import broker.angel as A                                            # noqa: E402


def scan(paused):
    clear()
    if paused:
        control.pause()
    f = FakeAngel()
    orig = f.request
    def req(method, url, headers=None, timeout=None, data=None):
        if url.endswith("getCandleData"):
            f.calls.append((url.replace(A.ROOT_URL, ""), json.loads(data), method, headers or {}))
            return Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": walk})
        return orig(method, url, headers=headers, timeout=timeout, data=data)
    f.request = req
    f.spot = walk[-1][4]
    cl, _, _ = make_client(f, paper=True)
    ocmod._kite_instruments_cache.clear(); ocmod._oi_snapshot.clear()
    saved = ocmod._fetch_with_retry
    ocmod._fetch_with_retry = lambda: (_ for _ in ()).throw(RuntimeError("NSE blocked"))
    main._get_kite = lambda: cl
    main.monitor.clear_trade(); main.strangle_legs = None
    fs = FinalSignal(action="CALL_SELL", strength="STRONG", score=3, lots=1, strike=round(f.spot / 50) * 50 + 100,
                     symbol=sym(EXP1, round(f.spot / 50) * 50 + 100, "CE"),
                     premium=opt_price(BY_SYMBOL[sym(EXP1, round(f.spot / 50) * 50 + 100, "CE")], f.spot),
                     sl_spot_level=f.spot + 60, expiry=EXP1.strftime("%d-%b-%Y"), spot_price=f.spot)
    main.combine_signals = lambda **kw: fs
    try:
        main.run_scan()
    finally:
        ocmod._fetch_with_retry = saved
    return cl


cl = scan(paused=False)
check("CONTROL (not paused): a forced strong signal inside run_scan opens a trade", main.monitor.trade is not None and len(sell_calls(cl)) == 1, str(cl.orders()))
main.monitor.clear_trade()
cl = scan(paused=True)
check("PAUSED: the same signal inside run_scan opens NOTHING and places NO order", main.monitor.trade is None and cl.orders() == [], str(cl.orders()))
main.monitor.clear_trade()

# the daily reminder
posts = []
main._post = lambda t: posts.append(t) or True
main._pause_reminded_date = None
clear(); main._pause_reminder()
check("no reminder while running", posts == [])
control.pause(); main._pause_reminder(); main._pause_reminder()
check("while paused: ONE reminder per day, telling how to resume", len(posts) == 1 and "/resume" in posts[0] and md_ok(posts[0]), str(posts))
clear()

# protection is independent of the pause: static guarantee on the source
src = Path(ROOT / "main.py").read_text()
gates = [m.start() for m in re.finditer(r"_paused_now\(\)", src)]
check("exactly three gates + the helper: pyramid add, reversal re-entry, new entry (nothing protective is gated)", len(gates) == 4, str(len(gates)))
for fn in ("guard_check", "eod_flatten", "verify_exit_fills", "verify_eod_flatten", "_chase_exit", "_reconcile_open_trade"):
    m = re.search(rf"^def {fn}\(.*?(?=^def |\Z)", src, re.S | re.M)
    check(f"{fn}() never consults the pause", m is not None and "_paused_now" not in m.group(0))

# ──────────────────────────────────────────────────────────────────────────────
print("\n[4] single-lot profile: allow_pyramiding, owner")
t = userprofile.render_template("Rahul", "angel", 500000, 5000, "new", 1, paper=False, allow_pyramiding=False, owner=True)
st = userprofile.build(userprofile.parse(t), {"MAX_START_LOTS": 3}, t)
z, v = st.sizing, st.config_values
check("1 lot: strong = moderate = strangle = 1", (z.lots_strong, z.lots_moderate, z.lots_strangle) == (1, 1, 1))
check("pyramiding off → every ceiling equals the starting size", (v["MAX_LOTS_DIRECTIONAL"], v["MAX_LOTS_STRANGLE_LEG"]) == (1, 1), str(v))
check("owner and live (paper = false) are carried", st.owner is True and st.paper is False and st.broker == "angel")
check("the user is told pyramiding is off", any("Pyramiding is OFF" in n for n in z.notes) and "Pyramiding is OFF" in onboarding.params_message(st))
t2 = userprofile.render_template("Priya", "angel", 500000, 5000, "new", 1)
st2 = userprofile.build(userprofile.parse(t2), {"MAX_START_LOTS": 3}, t2)
check("default profile: pyramiding on (ceiling 2×), not owner, Angel stays PAPER", st2.config_values["MAX_LOTS_DIRECTIONAL"] == 2 and st2.owner is False and st2.paper is True)
check("sizing.compute default unchanged for 3 lots (6 directional / 4 strangle leg)",
      (lambda s: (s.lots_strong, s.max_lots_directional, s.max_lots_strangle_leg))(sizing.compute(2_000_000, experience="experienced", max_start_lots=3)) == (3, 6, 4))
t4 = userprofile.render_template("Rahul", "angel", 200000, 2000, "new", 1, paper=False, allow_pyramiding=False, owner=True, loss_per_lot=2000)
st4 = userprofile.build(userprofile.parse(t4), {"MAX_START_LOTS": 3}, t4)
check("₹2L capital, ₹2K limit, 1 lot is accepted with loss_per_lot = 2000, and the risk is stated", st4.sizing.lots_strong == 1 and st4.sizing.daily_limit == 2000
      and any("past your daily limit" in w for w in st4.sizing.warnings) and md_ok(onboarding.params_message(st4)), str(st4.sizing.warnings))
try:
    t5 = userprofile.render_template("Rahul", "angel", 200000, 2000, "new", 1)
    userprofile.build(userprofile.parse(t5), {"MAX_START_LOTS": 3}, t5)
    refused = False
except Exception:
    refused = True
check("the same capital and limit WITHOUT that override is still refused", refused)
bad = userprofile.validate({"user": {"name": "x", "owner": "yes"}, "broker": {"name": "angel"}, "capital": {"total": 1}})
check("owner must be true/false", any("user.owner" in e for e in bad), str(bad))
bad = userprofile.validate({"user": {"name": "x"}, "broker": {"name": "angel"}, "capital": {"total": 1}, "risk": {"allow_pyramiding": 0}})
check("allow_pyramiding must be true/false (0 is refused)", any("allow_pyramiding" in e for e in bad), str(bad))
t3 = userprofile.render_template("Raj", "zerodha", 600000, None, "new", 1, owner=False, allow_pyramiding=False)
check("rendered template round-trips through the parser", userprofile.build(userprofile.parse(t3), {"MAX_START_LOTS": 3}, t3).config_values["MAX_LOTS_DIRECTIONAL"] == 1)

# ──────────────────────────────────────────────────────────────────────────────
print("\n[5] provision_user.py flags")
import importlib.util, io, contextlib                               # noqa: E402
spec = importlib.util.spec_from_file_location("provision_user", ROOT / "scripts" / "provision_user.py")
PU = importlib.util.module_from_spec(spec); spec.loader.exec_module(PU)
base = TMP / "users"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = PU.main(["rahul-angel", "--broker", "angel", "--live", "--lots", "1", "--capital", "500000", "--max-loss", "5000",
                  "--no-pyramiding", "--owner", "--base", str(base), "--no-seed"])
prof = (base / "rahul-angel" / "profile.toml").read_text()
check("provisioned: live, 1 lot, no pyramiding, owner — all in the file",
      rc == 0 and "paper = false" in prof and "allow_pyramiding = false" in prof and "owner = true" in prof and "preferred_lots = 1" in prof, prof)
check("its .env is private and has the Angel keys", (os.stat(base / "rahul-angel" / ".env").st_mode & 0o777) == 0o600 and "ANGEL_TOTP_SECRET=" in (base / "rahul-angel" / ".env").read_text())
check("the printout says live/paper clearly", "PAPER MODE" not in buf.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    PU.main(["plain", "--broker", "angel", "--capital", "500000", "--base", str(base), "--no-seed"])
p2 = (base / "plain" / "profile.toml").read_text()
check("without flags: paper (true), pyramiding on, not owner", "paper = true" in p2 and "allow_pyramiding" not in p2 and "owner" not in p2)

# ──────────────────────────────────────────────────────────────────────────────
print("\n[6] the live-Angel gate")
code = "AB1234"
now = datetime(2026, 10, 28, 11, 0)
paths.ANGEL_VERIFIED_FILE.unlink(missing_ok=True)
check("Zerodha is never gated", broker.live_gate("zerodha", False, "")[0])
check("Angel in paper mode is never gated", broker.live_gate("angel", True, code)[0])
ok, why = broker.live_gate("angel", False, code, now)
check("live Angel with no verification is REFUSED, with a reason", not ok and "verify_angel" in why, why)
broker.write_verified(code, now)
check("after a verification for this account: allowed", broker.live_gate("angel", False, code, now + timedelta(days=1))[0])
check("client code is case/space-insensitive", broker.live_gate("angel", False, " ab1234 ", now)[0])
ok, why = broker.live_gate("angel", False, "ZZ9999", now)
check("a verification for a DIFFERENT account does not count", not ok and "different" in why, why)
ok, why = broker.live_gate("angel", False, code, now + timedelta(days=31))
check("a verification older than 30 days expires", not ok and "30 days" in why, why)
check("exactly 30 days is still fine", broker.live_gate("angel", False, code, now + timedelta(days=30))[0])
paths.ANGEL_VERIFIED_FILE.write_text("garbage")
check("a corrupt marker is a refusal, not a crash", broker.live_gate("angel", False, code, now)[0] is False)
broker.write_verified(code, now)
check("the marker never contains the client code itself", code not in paths.ANGEL_VERIFIED_FILE.read_text())

spec = importlib.util.spec_from_file_location("verify_angel", ROOT / "scripts" / "verify_angel.py")
V = importlib.util.module_from_spec(spec); spec.loader.exec_module(V)
open_now = datetime(2026, 10, 28, 11, 0)           # a Wednesday, market open
paths.ANGEL_VERIFIED_FILE.unlink(missing_ok=True)
cl, fake, _ = make_client(FakeAngel()); out = []
res = V.run(cl, say=out.append, now=open_now)
check("a clean run in market hours with OI present is conclusive", V.conclusive(res), str([r for r in res if r[0] != V.OK]))
check("…and records the verification for exactly that account", V.record_if_conclusive(res, code, say=out.append) and broker.live_gate("angel", False, code)[0])
paths.ANGEL_VERIFIED_FILE.unlink(missing_ok=True)
cl, fake, _ = make_client(FakeAngel()); out = []
res = V.run(cl, say=out.append, now=datetime(2026, 10, 25, 11, 0))      # Sunday
check("a run on a closed market is NOT conclusive and records nothing", not V.conclusive(res) and not V.record_if_conclusive(res, code, say=out.append) and not paths.ANGEL_VERIFIED_FILE.exists())
res_bad = [(V.OK, "profile", ""), (V.BAD, "positions", "boom"), (V.OK, "option open interest", "x")]
check("any failure is not conclusive", not V.conclusive(res_bad))
res_nooi = [(V.OK, "profile", ""), (V.WARN, "option open interest", "0 of 5")]
check("no open interest seen is not conclusive (the bot's signals need it)", not V.conclusive(res_nooi))

# ──────────────────────────────────────────────────────────────────────────────
print("\n[7] Angel position sign cross-check")
cl, fake, _ = make_client()
fake.positions_raw = [
    {"tradingsymbol": "S1", "symboltoken": "1", "exchange": "NFO", "producttype": "INTRADAY", "buyqty": "0", "sellqty": "130", "netqty": "-130"},
    {"tradingsymbol": "S2", "symboltoken": "2", "exchange": "NFO", "producttype": "INTRADAY", "buyqty": "0", "sellqty": "130", "netqty": "130"},       # sign flipped by the broker
    {"tradingsymbol": "S3", "symboltoken": "3", "exchange": "NFO", "producttype": "INTRADAY", "buyqty": "130", "sellqty": "130", "netqty": "0"},       # closed
    {"tradingsymbol": "S4", "symboltoken": "4", "exchange": "NFO", "producttype": "INTRADAY", "buyqty": "65", "sellqty": "0"},                         # no netqty
    {"tradingsymbol": "S5", "symboltoken": "5", "exchange": "NFO", "producttype": "CARRYFORWARD", "buyqty": "0", "sellqty": "0", "netqty": "-65"},    # carried, day qty zero
]
pos = {x["tradingsymbol"]: x["quantity"] for x in cl.positions()["net"]}
check("consistent short stays negative, closed stays 0, missing netqty falls back", pos["S1"] == -130 and pos["S3"] == 0 and pos["S4"] == 65, str(pos))
check("a FLIPPED sign (netqty = −(buy−sell)) is overridden by the arithmetic: the short is a short", pos["S2"] == -130, str(pos))
check("the flip is counted so the owner can see it", cl.stats.get("sign_flips") == 1, str(cl.stats))
check("a carried position (day buy/sell zero) keeps the broker's netqty", pos["S5"] == -65, str(pos))

# ──────────────────────────────────────────────────────────────────────────────
print("\n[8] deploy file")
svc = (ROOT / "deploy" / "trading-bot@.service").read_text()
check("no restart loop for exit 2 (secrets) or 3 (live gate)", "RestartPreventExitStatus=2 3\n" in svc)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
