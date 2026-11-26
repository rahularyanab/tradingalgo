"""
Offline tests for the subscriber fleet (publish/fleet.py), the setup page (publish/setup_web.py) and the
bot's fleet commands. DigitalOcean, SSH and Telegram are faked — no droplet is created, nothing is sent.
Run from the repo root:   python3 tests/test_fleet.py
"""
import json, os, re, socket, sys, tempfile, threading, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
from publish import fleet as FL, setup_web as SW, telegram_live as T, executor_api as EA, x_poster as X

TMP = Path(tempfile.mkdtemp())
IST = FL.IST
CLOCK = [datetime(2026, 12, 1, 18, 0, tzinfo=IST)]
X.now_ist = lambda: CLOCK[0]
FL.now_ist = lambda: CLOCK[0]
FL.STATE, FL.LOCK, FL.SUBS = TMP / "fleet.json", TMP / "fleet.lock", TMP / "subs.json"
FL.TG_ENV = TMP / "tg.env"; FL.TG_ENV.write_text("TG_OWNER_ID=900\n")
FL.SSH_KEY = TMP / "ssh" / "fleet_key"
T.SUBS, T.STATE = FL.SUBS, TMP / "tg_state.json"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def at(d, hh, mm):
    CLOCK[0] = datetime(2026, 12, d, hh, mm, tzinfo=IST)


class FakeDO:
    def __init__(self):
        self.calls, self.status, self.n = [], "new", 0

    def ensure_ssh_key(self, pub):
        self.calls.append(("key", pub.split()[0]))
        return "fp:aa"

    def create_droplet(self, name, fp):
        self.calls.append(("create", name, fp))
        return 4242

    def droplet(self, did):
        return {"status": self.status, "ip": "10.0.0.7" if self.status == "active" else None}

    def count(self):
        return self.n

    def action(self, did, kind):
        self.calls.append((kind, did))

    def delete(self, did):
        self.calls.append(("delete", did))


class FakeSsh:
    def __init__(self):
        self.calls, self.env, self.check_ok, self.positions, self.log, self.up = [], "", True, "0", "", True

    def run(self, ip, cmd, stdin=None, timeout=600):
        self.calls.append((ip, cmd, stdin))
        if cmd == "true":
            return (0, "") if self.up else (255, "Connection refused")
        if "install_remote.sh" in cmd:
            return 0, "...\nINSTALLED\n"
        if cmd.startswith("umask 077"):
            self.env = stdin
            return 0, ""
        if cmd.startswith("grep '^EXEC_API_KEY="):
            m = re.search(r"^EXEC_API_KEY=.*$", self.env, re.M)
            return (0, m.group(0) + "\n") if m else (1, "")
        if "executor check" in cmd:
            return (0, "✓ settings\n✓ zerodha login works; Nifty 26000\nAll good.") if self.check_ok else \
                   (1, "✓ settings\n✓ executor API: plan small\n✗ zerodha login: Zerodha TOTP failed: Invalid TOTP")
        if "executor_state.json" in cmd:
            return 0, self.positions + "\n"
        if cmd.startswith("tail"):
            return 0, self.log
        if cmd.startswith("sed -i"):
            mode = re.search(r"MODE=(\w+)/", cmd).group(1)
            return 0, "1\n"
        return 0, ""

    def upload(self, ip, local, remote):
        files = sorted(str(p.relative_to(local)) for p in Path(local).rglob("*") if p.is_file())
        self.calls.append((ip, "UPLOAD", files))
        return 0, ""


class FakeLocal:
    def __init__(self):
        self.env, self.running, self.calls, self.logs = {}, set(), [], {}

    def write_env(self, cid, text):
        self.env[cid] = text

    def start(self, cid):
        self.running.add(cid); self.calls.append(("start", cid))

    def stop(self, cid):
        self.running.discard(cid); self.calls.append(("stop", cid))

    def log(self, cid):
        return self.logs.get(cid, "")

    def remove(self, cid):
        self.stop(cid); self.env.pop(cid, None); self.calls.append(("remove", cid))


SENT = []
notify = lambda cid, text, markup: SENT.append((str(cid), text, markup))
do, ssh, local = FakeDO(), FakeSsh(), FakeLocal()
FL.ssh_factory = lambda: ssh                       # the bot's /golive etc. use the fakes too
FL.local_factory = lambda: local
fleet = FL.Fleet(do, ssh, notify, {"max": 3, "local_max": 2, "setup_base": "https://setup.example"},
                 clock=lambda: CLOCK[0], local=local)
FL.settings = lambda: {"do_token": "", "max": 3, "local_max": 2, "setup_base": ""}      # no DO token / domain yet
FL.SUBS.write_text(json.dumps({"501": {"name": "Asha", "plan": "small", "username": "asha_k",
                                        "paid_until": "2027-01-01T18:00:00+05:30", "trial_until": "2026-11-20T00:00:00+05:30"},
                                "502": {"name": "Ravi", "plan": "standard", "trial_until": "2026-12-20T00:00:00+05:30"}}))

# ── 1. bot commands that start it ─────────────────────────────────────────────
print("owner starts a server")
side = []
send = lambda cid, text, markup=None: side.append((cid, text, markup))
ST = {"owner_id": "900", "channel": "@c"}
txt, _ = T.reply_for("/autotrade ravi zerodha", "900", "Rahul", ST, "", send)
check("/autotrade refuses a trial-only member", "isn't on paid access" in txt and not FL.load())
txt, _ = T.reply_for("/autotrade asha zerodha", "900", "Rahul", ST, "", send)
check("/autotrade on a paid member: a PAPER TRIAL on this server, no droplet", FL.load()["501"]["state"] == "local_requested"
      and "paper trial" in txt, txt)
txt, _ = T.reply_for("/autotrade asha zerodha", "501", "Asha", ST, "asha_k", send)
check("members can't run /autotrade", "🧪" not in txt)

print("paper trial on the main server")
at(1, 18, 0); fleet.tick()
r = FL.load()["501"]
env = local.env.get("501", "")
check("trial started: no DigitalOcean call at all", r["state"] == "local_paper" and do.calls == [] and "501" in local.running)
check("…PAPER, no broker login, prices from the owner's existing session", "MODE=paper" in env and "BROKER=paper" in env
      and "QUOTES_TOKEN_FILE=" in env and "KITE_API_KEY=" in env and "ZERODHA_PASSWORD" not in env, env)
k0 = re.search(r"^EXEC_API_KEY=(.*)$", env, re.M).group(1)
check("…its own key (hash stored, key only in its settings)", json.loads(FL.SUBS.read_text())["501"]["exec_key_sha256"] == EA.hash_key(k0))
check("…member told: nothing needed from them, evening summaries",
      any(m[0] == "501" and "nothing needed from you" in m[1] for m in SENT))
subs0 = json.loads(FL.SUBS.read_text())
subs0.update({"503": {"name": "Meera", "plan": "standard", "paid_until": "2027-01-01T00:00:00+05:30"},
              "504": {"name": "Dev", "plan": "small", "paid_until": "2027-01-01T00:00:00+05:30"}})
FL.SUBS.write_text(json.dumps(subs0))
FL.request("503", "angel"); FL.request("504", "zerodha")
fleet.tick()
st3, st4 = FL.load()["503"]["state"], FL.load()["504"]
check("trial cap (LOCAL_MAX=2): the third waits with a clear reason", st3 == "local_paper" and st4["state"] == "local_requested"
      and "LOCAL_MAX" in st4["error"], (st3, st4))
local.logs["501"] = ("2026-12-02 10:00:05,2  INFO     [PAPER] SELL NIFTY26D0826000CE x65 @ 180.0\n"
                     "2026-12-02 10:00:05,1  INFO     [PAPER] BUY NIFTY26D0827050CE x65 @ 5.0\n"
                     "2026-12-02 14:45:02,1  INFO     [PAPER] BUY NIFTY26D0826000CE x65 @ 151.25\n"
                     "2026-12-02 14:45:02,2  INFO     [PAPER] SELL NIFTY26D0827050CE x65 @ 2.9\n"
                     "2026-12-02 14:45:03,1  INFO     #9 BUY back NIFTY26D0826000CE 65/65 (PROFIT_LOCK)\n")
at(2, 15, 55); fleet.tick()
tsum = [m for m in SENT if m[0] == "501" and "Paper day" in m[1]]
check("15:50 trial summary from the local log, with the go-live button", tsum and "+₹1,732" in tsum[-1][1]
      and tsum[-1][2]["inline_keyboard"][0][0]["callback_data"] == "fleet:live", tsum[-1:] or SENT[-1])
txt, _ = T.reply_for("/golive asha", "900", "Rahul", ST, "", send)
check("/golive without DO token + domain: explained, trial keeps running", "needs a DigitalOcean token" in txt
      and "domain" in txt and FL.load()["501"]["state"] == "local_paper" and "501" in local.running, txt)
subs0 = json.loads(FL.SUBS.read_text()); subs0["503"]["paid_until"] = "2026-12-02T09:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs0))
fleet.tick()
check("trial member lapses → their paper executor stops", FL.load()["503"]["state"] == "off_local" and "503" not in local.running)
subs0["503"]["paid_until"] = "2027-01-01T00:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs0))
at(3, 9, 0); fleet.tick()
check("pays again → trial resumes", FL.load()["503"]["state"] == "local_paper" and "503" in local.running)
subs0["503"]["paid_until"] = "2026-12-03T10:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs0))
at(3, 11, 0); fleet.tick(); at(10, 11, 1); fleet.tick()
check("off 7 days → trial removed", FL.load()["503"]["state"] == "deleted" and ("remove", "503") in local.calls)
for cid in ("503", "504"):                                   # keep the rest of this file about 501 only
    with FL.locked():
        f_ = FL.load(); f_.pop(cid, None); FL.save(f_)
at(1, 18, 0)
FL.settings = lambda: {"do_token": "x", "max": 3, "local_max": 2, "setup_base": "https://setup.example"}
txt, _ = T.reply_for("/golive asha", "900", "Rahul", ST, "", send)
check("/golive with both: trial stopped, own server requested, member told", FL.load()["501"]["state"] == "requested"
      and "501" not in local.running and side[-1][0] == "501" and "your own trading server" in side[-1][1], txt)


# ── 2. the worker builds it ───────────────────────────────────────────────────
print("server lifecycle")
fleet.tick()
r = FL.load()["501"]
check("requested → creating: Bangalore, smallest size, our fleet key", r["state"] == "creating" and r["droplet_id"] == 4242
      and do.calls[0][0] == "key" and do.calls[1][0] == "create" and do.calls[1][1].startswith("td-exec-501"), do.calls)
check("…fleet SSH key generated on the main server", FL.SSH_KEY.exists())
fleet.tick()
r = FL.load()["501"]
check("droplet not ready: wait and retry later, not fail", r["state"] == "creating" and "not ready" in r["error"] and r.get("retry_at"))
do.status = "active"
CLOCK[0] += timedelta(minutes=5); fleet.tick()
check("active → installing, with its IP", FL.load()["501"]["state"] == "installing" and FL.load()["501"]["ip"] == "10.0.0.7")
ssh.up = False
CLOCK[0] += timedelta(minutes=1); fleet.tick()
check("SSH not up yet: retried later", FL.load()["501"]["state"] == "installing" and "SSH" in FL.load()["501"]["error"])
ssh.up = True
CLOCK[0] += timedelta(minutes=5); fleet.tick()
r = FL.load()["501"]
up = next(c for c in ssh.calls if c[1] == "UPLOAD")[2]
check("bundle uploaded: executor + Angel adapter + shims, NO strategy",
      "executor/engine.py" in up and "broker/angel.py" in up and "paths.py" in up and "install_remote.sh" in up
      and not any(x.startswith(("strategy", "signals", "publish", "main")) for x in up), up)
check("installer run with the main server's IP (droplet only accepts us)", any("install_remote.sh 159.223.87.53" in c[1] for c in ssh.calls))
key = re.search(r"^EXEC_API_KEY=(.*)$", ssh.env, re.M).group(1)
subs = json.loads(FL.SUBS.read_text())
check("a fresh key written ONLY to the droplet; main server keeps its hash", key.startswith("tdx_")
      and subs["501"]["exec_key_sha256"] == EA.hash_key(key) and key not in FL.SUBS.read_text() and key not in FL.STATE.read_text())
check("droplet starts in PAPER mode with no broker", "MODE=paper" in ssh.env and "BROKER=paper" in ssh.env)
guide = [m for m in SENT if m[0] == "501"][-1]
check("→ awaiting_broker; member gets the guide with THEIR IP and a setup-page button", r["state"] == "awaiting_broker"
      and "10.0.0.7" in guide[1] and "developers.kite.trade" in guide[1] and "TOTP" in guide[1]
      and guide[2]["inline_keyboard"][0][0]["url"].startswith("https://setup.example/s/")
      and guide[2]["inline_keyboard"][1][0]["callback_data"] == "fleet:call", guide)
check("owner told the server is ready", any(m[0] == "900" and "is ready" in m[1] for m in SENT))
link = guide[2]["inline_keyboard"][0][0]["url"]
token = link.rsplit("/", 1)[1]
check("setup link stored only as a hash", token not in FL.STATE.read_text() and FL.find_by_token(token) == "501")

# ── 3. the setup page ─────────────────────────────────────────────────────────
print("setup page")
clean, errs = SW.validate("zerodha", {"ZERODHA_USER_ID": "ab1234", "ZERODHA_PASSWORD": "secret12", "ZERODHA_TOTP_SECRET": "123456",
                                      "KITE_API_KEY": "abcdef123456", "KITE_API_SECRET": "abcdef1234567890abcd"})
check("6-digit code instead of the TOTP key → plain-language error", len(errs) == 1 and "not the 6-digit code" in errs[0], errs)
good = {"ZERODHA_USER_ID": "ab1234", "ZERODHA_PASSWORD": "secret12", "ZERODHA_TOTP_SECRET": "jbsw y3dp ehpk 3pxp",
        "KITE_API_KEY": "abcdef123456", "KITE_API_SECRET": "abcdef1234567890abcd"}
clean, errs = SW.validate("zerodha", good)
check("valid details pass; user id upper-cased, TOTP spaces removed", not errs and clean["ZERODHA_USER_ID"] == "AB1234"
      and clean["ZERODHA_TOTP_SECRET"] == "JBSWY3DPEHPK3PXP")
ssh.check_ok = False
ok, msgs = SW.submit("501", "zerodha", clean, ssh, notify)
check("broker rejects TOTP: friendly reason, still waiting, try counted", not ok and "TOTP key was not accepted" in msgs[0]
      and FL.load()["501"]["state"] == "awaiting_broker" and FL.load()["501"]["setup_tries"] == 1, msgs)
check("…the details went to the droplet with the key kept, broker set, still PAPER",
      "ZERODHA_TOTP_SECRET=JBSWY3DPEHPK3PXP" in ssh.env and f"EXEC_API_KEY={key}" in ssh.env and "BROKER=zerodha" in ssh.env
      and "MODE=paper" in ssh.env)
check("…and nothing of them on the main server", "JBSWY3DPEHPK3PXP" not in FL.STATE.read_text() + FL.SUBS.read_text()
      and "secret12" not in FL.STATE.read_text() + FL.SUBS.read_text())

# real HTTP round trip on localhost
ssh.check_ok = True
s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
httpd = SW.serve(port, ssh=ssh, notify=notify)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
try:
    urllib.request.urlopen(f"{base}/s/{'x' * 30}"); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("unknown link → 404 'expired'", code == 404)
pg = urllib.request.urlopen(f"{base}/s/{token}").read().decode()
check("form shows the 5 Zerodha boxes with help text, no secrets pre-filled", pg.count("<input") == 5 and "Zerodha user ID" in pg
      and "not the 6-digit code" in pg and "secret12" not in pg)
bad = urllib.request.urlopen(f"{base}/s/{token}", data=urllib.parse.urlencode({**good, "KITE_API_KEY": "!"}).encode()).read().decode()
check("bad field → the form again with the reason, password not echoed back", "API key doesn&#x27;t look right" in bad
      and "secret12" not in bad and FL.load()["501"]["state"] == "awaiting_broker", bad[-400:])
done = urllib.request.urlopen(f"{base}/s/{token}", data=urllib.parse.urlencode(good).encode()).read().decode()
check("good details → ✅ Connected; server now PAPER", "✅ Connected" in done and FL.load()["501"]["state"] == "paper")
check("member told paper starts next market day", any(m[0] == "501" and "paper mode" in m[1] for m in SENT))
try:
    urllib.request.urlopen(f"{base}/s/{token}"); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("the link can't be used again", code == 404 and FL.find_by_token(token) is None)
httpd.shutdown()

# ── 4. paper summary, going live ──────────────────────────────────────────────
print("paper days and going live")
ssh.log = ("2026-12-02 10:00:04,1  INFO     #7 BUY hedge NIFTY26D0827050CE 65/65\n"
           "2026-12-02 10:00:05,1  INFO     [PAPER] BUY NIFTY26D0827050CE x65 @ 5.0\n"
           "2026-12-02 10:00:05,2  INFO     [PAPER] SELL NIFTY26D0826000CE x65 @ 180.0\n"
           "2026-12-02 10:00:05,3  INFO     #7 SELL NIFTY26D0826000CE 65/65\n"
           "2026-12-02 14:45:02,1  INFO     [PAPER] BUY NIFTY26D0826000CE x65 @ 151.25\n"
           "2026-12-02 14:45:02,2  INFO     [PAPER] SELL NIFTY26D0827050CE x65 @ 2.9\n"
           "2026-12-02 14:45:03,1  INFO     #9 BUY back NIFTY26D0826000CE 65/65 (PROFIT_LOCK)\n"
           "2026-12-02 14:45:03,2  INFO     P1: flat\n")
with FL.locked():                                          # its trial already had a summary on 2 Dec in this test
    f_ = FL.load(); f_["501"].pop("last_summary", None); FL.save(f_)
mark = len(SENT)
at(2, 15, 0); fleet.tick()
check("no summary before 15:50", not any("Paper day" in m[1] for m in SENT[mark:]))
at(2, 15, 51); fleet.tick()
summ = [m for m in SENT[mark:] if "Paper day" in m[1]]
check("15:50 paper summary: actions + simulated P&L (+₹1,732) + go-live button", len(summ) == 1 and "+₹1,732" in summ[0][1]
      and "#9 BUY back" in summ[0][1] and summ[0][2]["inline_keyboard"][0][0]["callback_data"] == "fleet:live",
      summ[0][1] if summ else SENT[-1])
at(2, 16, 30); fleet.tick()
check("only once a day", len([m for m in SENT[mark:] if "Paper day" in m[1]]) == 1)
ans = T.on_callback("fleet:live", "501", ST, send)
check("'ready to go live' → owner gets /golive with their id", "quick check" in ans and side[-1][0] == "900" and "/golive 501" in side[-1][1])
ans = T.on_callback("fleet:call", "501", ST, send)
check("'book a call' → owner asked to propose a time", "15-minute call" in ans and "/msg 501" in side[-1][1])
txt, _ = T.reply_for("/golive asha", "900", "Rahul", ST, "", send)
check("/golive: live, capped at 1 lot, member told", "live, capped at 1 lot" in txt and FL.load()["501"]["state"] == "live"
      and FL.load()["501"]["cap_lots"] == 1 and side[-1][0] == "501" and "1 lot per trade" in side[-1][1]
      and "echo MAX_LOTS_PER_LEG=1 >>" in ssh.calls[-1][1] and "MODE=live/" in ssh.calls[-1][1], ssh.calls[-1][1])
txt, _ = T.reply_for("/fullsize asha", "900", "Rahul", ST, "", send)
check("/fullsize lifts the cap", "live" in txt and FL.load()["501"]["cap_lots"] is None)
txt, _ = T.reply_for("/fleet", "900", "Rahul", ST, "", send)
check("/fleet lists the server", "<code>501</code> Asha · zerodha · <b>live</b> · 10.0.0.7" in txt, txt)

# ── 5. lapse, renewal, deletion ───────────────────────────────────────────────
print("lapse and renewal")
subs = json.loads(FL.SUBS.read_text()); subs["501"]["paid_until"] = "2026-12-03T09:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs))
ssh.positions = "1"
at(3, 11, 0); fleet.tick()
check("lapsed but holding a position mid-day: keeps running (exits still flow)", FL.load()["501"]["state"] == "live")
ssh.positions = "0"
at(3, 12, 0); fleet.tick()
check("lapsed and flat, but still market hours: keeps running until 15:30", FL.load()["501"]["state"] == "live")
at(3, 15, 31); fleet.tick()
r = FL.load()["501"]
check("after 15:30, flat → service stopped, droplet powered off, owner told", r["state"] == "off" and ("power_off", 4242) in do.calls
      and any("powered off" in m[1] for m in SENT if m[0] == "900"))
subs["501"]["paid_until"] = "2027-02-01T00:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs))
at(4, 9, 0); fleet.tick()
check("paid again → powered back on", FL.load()["501"]["state"] == "resuming" and ("power_on", 4242) in do.calls)
fleet.tick()
check("…and back to LIVE as before", FL.load()["501"]["state"] == "live")
subs["501"]["paid_until"] = "2026-12-04T10:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs))
at(4, 15, 40); fleet.tick()
check("lapses again → off", FL.load()["501"]["state"] == "off")
at(10, 15, 0); fleet.tick()
check("off 6 days: kept", FL.load()["501"]["state"] == "off")
at(11, 15, 41); fleet.tick()
check("off 7 days: droplet deleted, owner told", FL.load()["501"]["state"] == "deleted" and ("delete", 4242) in do.calls)

print("budget and bundle")
do.n = 3
subs["502"]["paid_until"] = "2027-01-01T00:00:00+05:30"; FL.SUBS.write_text(json.dumps(subs))
FL.request("502", "angel", local=False)                    # straight to an own droplet
fleet.tick()
check("droplet budget reached → nothing created, error shown in /fleet", FL.load()["502"]["state"] == "requested"
      and "budget" in FL.load()["502"]["error"] and not any(c[0] == "create" and "502" in c[1] for c in do.calls))
b = FL.build_bundle()
names = sorted(str(p.relative_to(b)) for p in b.rglob("*") if p.is_file())
check("bundle: only executor, Angel adapter, shims, installer", all(n.split("/")[0] in ("executor", "broker") or n in
      ("paths.py", "requirements.txt", "executor.service", "install_remote.sh") for n in names) and "executor/engine.py" in names, names)
import shutil; shutil.rmtree(b)
check("Angel guide asks for client code, PIN, TOTP, API key", all(x in FL.guide_text("angel", "1.2.3.4") for x in
      ("smartapi.angelbroking.com", "1.2.3.4", "PIN", "TOTP")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
