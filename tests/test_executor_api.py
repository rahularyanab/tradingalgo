"""
Offline tests for the executor instruction feed (publish/executor_feed.py) and API (publish/executor_api.py),
including a real HTTPS round trip on localhost with a throwaway self-signed certificate.
No broker, no Telegram, nothing leaves this machine. Run from the repo root:
    python3 tests/test_executor_api.py
"""
import hashlib, json, os, socket, ssl, subprocess, sys, tempfile, threading, urllib.request
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import signal_feed as feed, executor_feed as EF, executor_api as EA, telegram_live as T, x_poster as X
from signals.trade_monitor import TradeState

TMP = Path(tempfile.mkdtemp())
LOT = feed.NIFTY_LOT_SIZE
IST = X.IST
CLOCK = [datetime(2026, 11, 27, 9, 0, tzinfo=IST)]
feed._now = lambda: CLOCK[0]
X.now_ist = lambda: CLOCK[0]
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def reset(tag):
    feed.OUTBOX = TMP / f"{tag}_outbox.jsonl"
    feed.STATE = TMP / f"{tag}_feed.json"
    EF.INSTRUCTIONS, EF.STATE = TMP / f"{tag}_ins.jsonl", TMP / f"{tag}_exec.json"
    EA.SUBS = T.SUBS = TMP / f"{tag}_subs.json"
    EA.TG_ENV = TMP / f"{tag}_tg.env"
    EA.TG_ENV.write_text("TG_OWNER_ID=900\n")
    feed._mark = None
    EF.update(feed.OUTBOX)                                   # first run: cursor at the end


def at(hh, mm, d=27):
    CLOCK[0] = datetime(2026, 11, d, hh, mm, tzinfo=IST)


def sync():
    EF.update(feed.OUTBOX)


def call_sell(lots, strike=26000, prem=180.0, hedge=27050, hprem=5.0, sl=26050):
    return TradeState(action="CALL_SELL", strike=strike, symbol=f"NIFTY26D01{strike}CE", entry_time=CLOCK[0],
                      entry_premium=prem, entry_spot=25945, sl_spot_level=sl, expiry="01-Dec-2026",
                      lots=lots * LOT, hedge_symbol=f"NIFTY26D01{hedge}CE", hedge_strike=hedge, hedge_entry_premium=hprem)


def brief(orders):
    return [(o["side"], o["role"], o["lots"], o["qty"]) for o in orders]


# ── 1. instructions from Rahul's real 26 Nov day ──────────────────────────────
print("instructions: real 26 Nov trades")
reset("real")
at(10, 30); t1 = call_sell(2, prem=167.80, hedge=27100, hprem=5.30, sl=26020)
feed.trade_entry(t1, 25917, strength="MODERATE"); sync()
at(12, 0); feed.trade_exit(t1, 172.80, -812.0, "PROFIT_LOCK", 25935); sync()
at(12, 15); t2 = call_sell(2); feed.trade_entry(t2, 25945, strength="STRONG"); sync()
at(14, 0); feed.trade_exit(replace(t2, lots=LOT), 96.20, 5346.0, "PROFIT_BOOK", 25780); sync()
at(14, 45); t2.lots = LOT; feed.trade_exit(t2, 151.25, 1732.0, "PROFIT_LOCK", 25908); sync()
ins = EF.read_since(0)
check("5 instructions: OPEN, CLOSE, OPEN, ADJUST, CLOSE", [i["kind"] for i in ins] == ["OPEN", "CLOSE", "OPEN", "ADJUST", "CLOSE"],
      [i["kind"] for i in ins])
o = ins[0]["plans"]["standard"]["orders"]
check("open (moderate → 2 lots): BUY hedge first, then SELL main", brief(o) == [("BUY", "hedge", 2, 130), ("SELL", "main", 2, 130)]
      and o[0]["symbol"] == "NIFTY26D0127100CE" and o[1]["symbol"] == "NIFTY26D0126000CE", o)
check("small plan opens 1 lot", brief(ins[0]["plans"]["small"]["orders"]) == [("BUY", "hedge", 1, 65), ("SELL", "main", 1, 65)])
check("stop and flatten time travel with it", ins[0]["sl"] == {"spot_above": 26020} and ins[0]["flatten_by"] == "15:25")
check("close: BUY BACK main first, then SELL hedge", brief(ins[1]["plans"]["standard"]["orders"]) == [("BUY", "main", 2, 130), ("SELL", "hedge", 2, 130)]
      and all(x["closing"] for x in ins[1]["plans"]["standard"]["orders"]))
check("strong → 3 lots", brief(ins[2]["plans"]["standard"]["orders"]) == [("BUY", "hedge", 3, 195), ("SELL", "main", 3, 195)])
check("owner books 1 of 2 → standard books 2 of 3 (same as the channel)", brief(ins[3]["plans"]["standard"]["orders"]) == [("BUY", "main", 2, 130), ("SELL", "hedge", 2, 130)]
      and ins[3]["plans"]["standard"]["open_after"] == {"CE": 1})
check("…small holds its lot (no orders)", ins[3]["plans"]["small"]["orders"] == [] and ins[3]["plans"]["small"]["open_after"] == {"CE": 1})
check("final close: standard 1, small 1", brief(ins[4]["plans"]["standard"]["orders"])[0] == ("BUY", "main", 1, 65)
      and brief(ins[4]["plans"]["small"]["orders"])[0] == ("BUY", "main", 1, 65))
check("first run never replays old trades", json.loads(EF.STATE.read_text())["seq"] == 5)

print("instructions: adds and strangles")
reset("adds")
at(10, 0); t = call_sell(6); feed.trade_entry(t, 25900, strength="STRONG"); sync()
at(11, 0); t.lots = 12 * LOT; feed.trade_add(t, 6 * LOT, [{"leg": "CE", "strike": 26000, "price": 150.0}]); sync()
add = EF.read_since(0)[-1]
check("pyramid add: hedge BUY first, then SELL +3 (3 → 6)", brief(add["plans"]["standard"]["orders"]) == [("BUY", "hedge", 3, 195), ("SELL", "main", 3, 195)]
      and add["kind"] == "ADJUST", add["plans"]["standard"])
s = TradeState(action="STRANGLE", strike=26200, symbol="x", entry_time=CLOCK[0], entry_premium=80.0, entry_spot=25900,
               sl_spot_level=26250, sl_put=25600, expiry="01-Dec-2026", lots=4 * LOT)
reset("str")
at(10, 0); feed.trade_entry(s, 25900, strength="STRANGLE", strangle={"ce_strike": 26200, "ce_symbol": "NC", "ce_price": 45.0,
                                                                    "pe_strike": 25600, "pe_symbol": "NP", "pe_price": 35.0}); sync()
at(11, 0); feed.trade_exit(replace(s, action="STRANGLE_CE", strike=26200), 70.0, -6500.0, "SL_HIT", 26260); sync()
st_ = EF.read_since(0)
check("strangle opens 1 lot each leg, no hedge", brief(st_[0]["plans"]["standard"]["orders"]) == [("SELL", "main", 1, 65), ("SELL", "main", 1, 65)]
      and st_[0]["sl"] == {"spot_above": 26250, "spot_below": 25600})
check("CE leg stop: buy back CE only, PE stays", [(o["side"], o["leg"]) for o in st_[1]["plans"]["standard"]["orders"]] == [("BUY", "CE")]
      and st_[1]["plans"]["standard"]["open_after"] == {"CE": 0, "PE": 1} and st_[1]["kind"] == "ADJUST")

# ── 2. who gets what ──────────────────────────────────────────────────────────
print("access")
reset("acc")
now = datetime(2026, 11, 27, 9, 0, tzinfo=IST)
subs = {"501": {"name": "Asha", "plan": "small", "paid_until": "2026-12-27T09:00:00+05:30"},
        "502": {"name": "Ravi", "plan": "standard", "trial_until": "2026-12-20T09:00:00+05:30"},
        "900": {"name": "Rahul", "plan": "standard"}}
keys = {}
for cid in subs:
    k, h = EA.new_key()
    keys[cid] = k
    subs[cid].update(exec_key_sha256=h, exec_key_created="2026-11-26T20:00:00+05:30")
EA.SUBS.write_text(json.dumps(subs))
check("keys are stored only as hashes", all("tdx_" not in json.dumps(v) for v in subs.values())
      and subs["501"]["exec_key_sha256"] == hashlib.sha256(keys["501"].encode()).hexdigest())
check("key lookup: right key → right member; wrong/empty → none",
      EA.who(keys["501"])[0] == "501" and EA.who("tdx_nope") is None and EA.who("") is None)
at(10, 0); t = call_sell(2); feed.trade_entry(t, 25900, strength="STRONG"); sync()
r = EA.instructions_for(subs["501"], False, 0, CLOCK[0])
check("paid member gets their plan's orders (small → 1 lot)", r["status"] == "paid" and len(r["instructions"]) == 1
      and brief(r["instructions"][0]["orders"]) == [("BUY", "hedge", 1, 65), ("SELL", "main", 1, 65)], r)
r = EA.instructions_for(subs["502"], False, 0, CLOCK[0])
check("trial-only member: no auto-trading", r["status"] == "inactive" and r["instructions"] == [] and r["next"] == 1)
r = EA.instructions_for(subs["900"], True, 0, CLOCK[0])
check("owner: standard 3 lots", r["status"] == "owner" and brief(r["instructions"][0]["orders"])[1] == ("SELL", "main", 3, 195))
subs["501"]["paid_until"] = "2026-11-27T10:30:00+05:30"          # /revoke at 10:30, mid-trade
at(11, 0); feed.trade_exit(t, 120.0, 7800.0, "TARGET", 25850); sync()          # the 10:00 trade closes
at(11, 30); t2 = call_sell(2, strike=26100); feed.trade_entry(t2, 25950, strength="STRONG"); sync()
at(12, 0); feed.trade_exit(t2, 120.0, 3900.0, "TARGET", 25850); sync()
r = EA.instructions_for(subs["501"], False, 1, CLOCK[0])
check("lapsed mid-trade: the exit of the trade opened while paid IS sent", len(r["instructions"]) == 1
      and r["instructions"][0]["kind"] == "CLOSE" and r["instructions"][0]["position_id"] == EF.read_since(0)[0]["position_id"]
      and brief(r["instructions"][0]["orders"])[0] == ("BUY", "main", 1, 65), r["instructions"])
check("…but the 11:30 trade and its exit are NOT", r["next"] == 4)
subs["503"] = {"name": "New", "plan": "standard", "paid_until": "2026-12-30T00:00:00+05:30",
               "exec_key_sha256": "x", "exec_since": "2026-11-27T11:15:00+05:30"}
r = EA.instructions_for(subs["503"], False, 0, CLOCK[0])
check("key issued at 11:15: nothing from before it (no orphan close of a trade they never opened)",
      [i["kind"] for i in r["instructions"]] == ["OPEN", "CLOSE"]
      and r["instructions"][0]["position_id"] == EF.read_since(0)[2]["position_id"], [i["kind"] for i in r["instructions"]])

# ── 3. owner /key command ─────────────────────────────────────────────────────
print("owner /key")
EA.SUBS.write_text(json.dumps({"501": {"name": "Asha", "plan": "small", "paid_until": "2026-12-27T09:00:00+05:30",
                                        "trial_until": "2026-12-01T00:00:00+05:30"},
                                "502": {"name": "Ravi", "plan": "standard", "trial_until": "2026-12-20T09:00:00+05:30"}}))
ST = {"owner_id": "900", "channel": "@c"}
txt, _ = T.reply_for("/key asha", "900", "Rahul", ST)
key = txt.split("<code>")[1].split("</code>")[0]
saved = json.loads(EA.SUBS.read_text())["501"]
check("/key: key shown once to the owner, only its hash saved", key.startswith("tdx_") and len(key) > 40
      and saved["exec_key_sha256"] == EA.hash_key(key) and key not in EA.SUBS.read_text(), txt)
check("…and it authenticates", EA.who(key)[0] == "501")
txt2, _ = T.reply_for("/key asha", "900", "Rahul", ST)
check("a new /key replaces the old one", EA.who(key) is None and "Any older key" in txt2)
txt, _ = T.reply_for("/key ravi", "900", "Rahul", ST)
check("trial-only member can't get a key", "isn't on paid access" in txt and "exec_key_sha256" not in json.loads(EA.SUBS.read_text())["502"])
txt, _ = T.reply_for("/key asha off", "900", "Rahul", ST)
check("/key off removes it, and warns to prefer /revoke mid-trade", "use /revoke instead" in txt
      and "exec_key_sha256" not in json.loads(EA.SUBS.read_text())["501"])
txt, _ = T.reply_for("/key asha", "501", "Asha", ST)
check("members can't issue keys", "🔑" not in txt)

# ── 4. real HTTPS round trip with a pinned self-signed certificate ────────────
print("HTTPS")
tls = TMP / "tls"; tls.mkdir()
subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-keyout", str(tls / "key.pem"), "-out", str(tls / "cert.pem"), "-days", "2", "-subj", "/CN=test",
                "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
reset("https")
k, h = EA.new_key()
EA.SUBS.write_text(json.dumps({"501": {"name": "Asha", "plan": "small", "paid_until": "2030-01-01T00:00:00+05:30",
                                        "exec_key_sha256": h, "exec_key_created": "2026-01-01T00:00:00+05:30"}}))
at(10, 0); feed.trade_entry(call_sell(2), 25900, strength="STRONG"); sync()
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
httpd = EA.serve(port=port, tls_dir=tls, host="127.0.0.1")
threading.Thread(target=httpd.serve_forever, daemon=True).start()
pem = (tls / "cert.pem").read_text()
pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def get(path, key=None, pin_expected=pin):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                       # trust comes from the pinned fingerprint below
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
        got = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
        if got != pin_expected:
            return "PIN MISMATCH", None
        req = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n" + (f"Authorization: Bearer {key}\r\n" if key else "") + "\r\n"
        s.sendall(req.encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


code, body = get("/v1/status", k)
check("HTTPS /v1/status with the key: 200, plan + latest_seq", code == 200 and body["plan"] == "small" and body["latest_seq"] == 1, body)
code, body = get("/v1/instructions?since=0", k)
check("HTTPS /v1/instructions: the OPEN at 1 lot", code == 200 and brief(body["instructions"][0]["orders"]) == [("BUY", "hedge", 1, 65), ("SELL", "main", 1, 65)], body)
code, body = get("/v1/instructions?since=0")
check("no key → 401", code == 401 and body["ok"] is False)
code, body = get("/v1/instructions?since=0", "tdx_wrong")
check("wrong key → 401", code == 401)
code, body = get("/v1/instructions?since=abc", k)
check("bad cursor → 400", code == 400)
check("pinning: a different certificate is refused", get("/v1/status", k, pin_expected="0" * 64)[0] == "PIN MISMATCH")
check("request lines (with keys) are never logged", EA.Handler.log_message(None, "%s", "x") is None)
httpd.shutdown()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
