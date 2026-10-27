"""
Offline tests for the per-user profile, lot sizing, isolation, onboarding and provisioning.

Run from the repo root:   python3 tests/test_user_profile.py
Nothing here talks to a broker or to Telegram.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("ALGO_USER_DIR", None)                # this process plays the OWNER's default instance

import sizing                                        # noqa: E402
import userprofile                                   # noqa: E402
from sizing import ProfileError                      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def md_ok(text):
    if text.count("`") % 2:
        return False
    plain = re.sub(r"`[^`]*`", "", text)
    return plain.count("_") % 2 == 0 and plain.count("*") % 2 == 0


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except ProfileError as e:
        return str(e)
    return None


L = 100_000
print("\n[1] lot suggestion matches the published table")
new = {c: sizing.compute(c * L).suggested_lots for c in (5, 10, 13, 20)}
exp = {c: sizing.compute(c * L, experience="experienced").suggested_lots for c in (5, 10, 13, 20)}
check("new user: ₹5L→1, ₹10L→2, ₹13L→2, ₹20L→3 (capped at the tested size)", new == {5: 1, 10: 2, 13: 2, 20: 3}, str(new))
check("experienced: ₹5L→1, ₹10L→2, ₹13L→3, ₹20L→3", exp == {5: 1, 10: 2, 13: 3, 20: 3}, str(exp))
z = sizing.compute(13 * L)
check("default daily limit = 1% of capital (₹13,000), risk rule 2, margin rule 7", z.daily_limit == 13000 and z.risk_lots == 2 and z.margin_lots == 7, str((z.daily_limit, z.risk_lots, z.margin_lots)))
check("margin used at the starting size is a modest share of capital", z.margin_at_start / z.capital < 0.2, str(z.margin_at_start))
check("raising the start cap lifts a rich account above 3 lots", sizing.compute(20 * L, max_start_lots=6).suggested_lots == 4)

print("\n[2] daily-limit rules")
check("above 1.5% of capital is refused, with the numbers", "1.5%" in (raises(sizing.compute, 10 * L, max_daily_loss=20000) or ""))
r = sizing.compute(10 * L, max_daily_loss=12000)
check("between 1% and 1.5% is allowed with a warning", r.daily_limit == 12000 and any("above 1%" in w for w in r.warnings), str(r.warnings))
check("exactly 1.5% is allowed", sizing.compute(10 * L, max_daily_loss=15000).daily_limit == 15000)
check("a limit tighter than 1% is fine", sizing.compute(10 * L, max_daily_loss=5000).suggested_lots == 1)
check("zero / negative / text limits are refused", all(raises(sizing.compute, 10 * L, max_daily_loss=v) for v in (0, -5, "abc")))

print("\n[3] capital too small")
e = raises(sizing.compute, 200_000)
check("₹2L is refused and the message says the minimum", e and "too small" in e and "₹" in e, str(e))
check("₹4L works only if the user accepts a bigger limit (1.5%)", raises(sizing.compute, 400_000) is not None and sizing.compute(400_000, max_daily_loss=6000).suggested_lots == 1)
check("zero / negative capital refused", raises(sizing.compute, 0) and raises(sizing.compute, -1))

print("\n[4] preferred lots")
check("below the suggestion: honoured", sizing.compute(13 * L, preferred_lots=1).effective_lots == 1)
r = sizing.compute(13 * L, preferred_lots=3)
check("above the suggestion: capped, and the user is told why", r.effective_lots == 2 and any("asked for 3" in w for w in r.warnings), str(r.warnings))
r = sizing.compute(13 * L, preferred_lots=3, allow_above_suggestion=True)
check("above the suggestion with the override: honoured, with a loud warning", r.effective_lots == 3 and any("above our suggestion" in w for w in r.warnings))
check("override never exceeds the bot's own ceiling of 6", sizing.compute(50 * L, preferred_lots=9, allow_above_suggestion=True).effective_lots == 6)
check("0, 2.5, true, text refused", all(raises(sizing.compute, 13 * L, preferred_lots=v) for v in (0, 2.5, True, "2")))

print("\n[5] everything scales together — and at 3 lots equals today's bot exactly")
import config                                        # the default instance (no profile)
z3 = sizing.compute(13 * L, preferred_lots=3, allow_above_suggestion=True, experience="experienced")
check("default instance has no profile", config.SETTINGS is None)
same = {
    "LOTS_STRONG": z3.lots_strong, "LOTS_MODERATE": z3.lots_moderate, "LOTS_STRANGLE": z3.lots_strangle,
    "MAX_LOTS_DIRECTIONAL": z3.max_lots_directional, "MAX_LOTS_STRANGLE_LEG": z3.max_lots_strangle_leg,
    "PROTECTED_MAX_LOTS": z3.protected_max_lots, "PYRAMID_STEP_LOTS": z3.step_lots, "PROFIT_BOOK_STEP_LOTS": z3.step_lots,
    **z3.thresholds,
}
diff = {k: (v, getattr(config, k)) for k, v in same.items() if getattr(config, k) != v}
check("at 3 lots every derived setting equals what the owner's bot already uses", not diff, str(diff))
z1 = sizing.compute(5 * L)
check("at 1 lot: strong 1 / moderate 1 / strangle 1 / ceiling 2 / step 1", (z1.lots_strong, z1.lots_moderate, z1.lots_strangle, z1.max_lots_directional, z1.step_lots) == (1, 1, 1, 2, 1))
check("rupee thresholds scale with size (1 lot ≈ a third of the 3-lot values)", z1.thresholds["PROFIT_BOOK_STEP1_PNL"] == 1700 and z1.thresholds["PROFIT_LOCK_ARM_PNL"] == 500, str(z1.thresholds))

print("\n[6] profile file validation")
GOOD = """
[user]
name = "Priya"
[broker]
name = "zerodha"
[capital]
total = 1000000
"""
st = userprofile.build(userprofile.parse(GOOD), vars(config), GOOD)
check("a minimal valid profile builds", st.name == "Priya" and st.sizing.daily_limit == 10000 and st.config_values["DAILY_MAX_LOSS"] == 10000)


def problems(text):
    return raises(userprofile.build, userprofile.parse(text), vars(config), text) or ""


check("missing capital is reported", "Missing required setting: [capital] total" in problems('[broker]\nname="zerodha"\n'))
check("unknown section and unknown key are reported", "Unknown section [bogus]" in problems(GOOD + "\n[bogus]\nx=1\n") and "Unknown setting 'colour'" in problems(GOOD + '\n[risk]\ncolour="red"\n'))
check("wrong types are reported in plain words", "must be a number" in problems(GOOD.replace("1000000", '"lots"')) and "true or false" in problems(GOOD + "\n[risk]\nallow_above_suggestion = 1\n"))
check("TOML true is not accepted as a number", "must be a number" in problems(GOOD.replace("1000000", "true")))
check('"angel" is now an accepted broker (and defaults to paper mode — see test_angel_adapter.py)', problems(GOOD.replace("zerodha", "angel")) == "")
check("an unknown broker is refused", "not supported" in problems(GOOD.replace("zerodha", "fyers")))
check("several problems are listed together", problems('[capital]\ntotal="x"\n[risk]\nzzz=1\n').count("\n  - ") >= 3)
check("invalid TOML gives a readable error", "not valid TOML" in (raises(userprofile.parse, "[capital\ntotal=1") or ""))

print("\n[7] owner overrides")
ov = GOOD + "\n[overrides]\nPROFIT_LOCK_GIVEBACK_PCT = 0.4\nMAX_LOTS_DIRECTIONAL = 4\n"
st2 = userprofile.build(userprofile.parse(ov), vars(config), ov)
check("a known setting is applied and wins over the computed value", st2.config_values["PROFIT_LOCK_GIVEBACK_PCT"] == 0.4 and st2.config_values["MAX_LOTS_DIRECTIONAL"] == 4)
check("an unknown setting is refused", "no such setting" in problems(GOOD + "\n[overrides]\nNOT_A_THING = 1\n"))
check("a wrong type is refused", "expected" in problems(GOOD + '\n[overrides]\nDAILY_MAX_LOSS = "lots"\n'))
check("a lowercase name is refused", "no such setting" in problems(GOOD + "\n[overrides]\ndaily_max_loss = 1\n"))

print("\n[8] isolation: one person's folder never leaks into another's or the owner's")
tmp = Path(tempfile.mkdtemp())
u = tmp / "priya"
(u / "logs").mkdir(parents=True); (u / "data").mkdir()
(u / "profile.toml").write_text(userprofile.render_template("Priya", "zerodha", 1_000_000, None, "new", None))
(u / ".env").write_text("TELEGRAM_BOT_TOKEN=USERTOKEN\nTELEGRAM_CHAT_ID=USERCHAT\nKITE_API_KEY=USERKEY\n")
probe = ("import json, config, paths;"
         "print(json.dumps(dict(chat=config.TELEGRAM_CHAT_ID, tok=config.TELEGRAM_BOT_TOKEN, key=config.KITE_API_KEY,"
         "limit=config.DAILY_MAX_LOSS, strong=config.LOTS_STRONG, name=getattr(config.SETTINGS,'name',None),"
         "state=str(paths.STATE_FILE), journal=str(paths.JOURNAL_FILE), sr=str(paths.SR_DB_FILE), token=str(paths.TOKEN_FILE), log=str(paths.BOT_LOG))))")


def run_probe(env_extra):
    env = {**os.environ, **env_extra}
    r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True)
    return r, (json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else None)


r, p = run_probe({"ALGO_USER_DIR": str(u), "TELEGRAM_CHAT_ID": "LEAKED-FROM-PARENT", "KITE_API_KEY": "LEAKED-KEY"})
check("the user's .env wins over anything already in the process environment", p and p["chat"] == "USERCHAT" and p["key"] == "USERKEY" and p["tok"] == "USERTOKEN", r.stderr[-300:] if not p else str(p))
check("the profile is applied (₹10,000 limit, 2 lots at ₹10L)", p and p["limit"] == 10000 and p["strong"] == 2 and p["name"] == "Priya", str(p))
check("state, journal, S/R, token and log all live inside the user's folder",
      p and all(str(u) in p[k] for k in ("state", "journal", "sr", "token", "log")), str(p))
(u / ".env").write_text("TELEGRAM_BOT_TOKEN=USERTOKEN\n")                      # a .env that OMITS the chat id and the broker key
r, p = run_probe({"ALGO_USER_DIR": str(u), "TELEGRAM_CHAT_ID": "OWNERS-CHAT", "KITE_API_KEY": "OWNERS-KEY", "ZERODHA_PASSWORD": "OWNERS-PW"})
check("a key missing from the user's .env does NOT fall back to the owner's (alerts could reach the owner's chat)",
      p and p["chat"] == "" and p["key"] == "" and p["tok"] == "USERTOKEN", r.stderr[-300:] if not p else str(p))
(u / ".env").write_text("TELEGRAM_BOT_TOKEN=USERTOKEN\nTELEGRAM_CHAT_ID=USERCHAT\nKITE_API_KEY=USERKEY\n")
(u / "profile.toml").unlink()
r, p = run_probe({"ALGO_USER_DIR": str(u)})
check("a user folder with no profile refuses to start (never falls back to the owner's setup)", r.returncode != 0 and "does not exist" in (r.stderr + r.stdout), r.stderr[-200:])
r, p = run_probe({})
import config as _c
check("with no ALGO_USER_DIR the paths are exactly the owner's existing layout",
      p and p["state"] == str(ROOT / "logs" / "algo_trade_state.json") and p["journal"] == str(ROOT / "logs" / "trade_journal.csv")
      and p["sr"] == str(ROOT / "data" / "sr_levels.json") and p["token"] == str(ROOT / ".access_token") and p["log"] == str(ROOT / "logs" / "bot.log"), str(p))
check("...and the owner's limits are untouched (5000 / 3 lots, no profile)", p and p["limit"] == 5000 and p["strong"] == 3 and p["name"] is None, str(p))

print("\n[9] secrets check")
check("everything present → nothing missing", userprofile.missing_secrets("zerodha", {k: "x" for k in userprofile.ALWAYS_REQUIRED_SECRETS + userprofile.REQUIRED_SECRETS["zerodha"]}) == [])
m = userprofile.missing_secrets("zerodha", {"TELEGRAM_BOT_TOKEN": "x", "KITE_API_KEY": " "})
check("Telegram and broker keys are mandatory; blanks count as missing", "TELEGRAM_CHAT_ID" in m and "KITE_API_KEY" in m and "TELEGRAM_BOT_TOKEN" not in m, str(m))

print("\n[10] Telegram onboarding")
from notifications import onboarding                 # noqa: E402
import paths                                         # noqa: E402
st = userprofile.build(userprofile.parse(GOOD), vars(config), GOOD)
msgs = onboarding.welcome_messages(st)
check("three messages, each safely under Telegram's 4096 limit", len(msgs) == 3 and all(len(m) < 3800 for m in msgs), str([len(m) for m in msgs]))
check("every message is valid legacy Markdown (no unpaired _ * `)", all(md_ok(m) for m in msgs), str([i for i, m in enumerate(msgs) if not md_ok(m)]))
check("the guide states the user's own numbers and the 'not a guarantee' warning",
      "₹10,00,000" in msgs[0].replace(",000,000", ",00,000") or "10,00,000" in msgs[0] or "1,000,000" in msgs[0]
      if False else ("Daily loss limit" in msgs[0] and "not a guarantee" in msgs[2] and "10,000" in msgs[0]), msgs[0][:300])
check("it lists the commands", all(c in msgs[2] for c in ("/params", "/pnl", "/sr", "/start", "/help")))
w = sizing.compute(13 * L, preferred_lots=3)
st_w = userprofile.build(userprofile.parse(GOOD.replace("1000000", "1300000") + "\n[risk]\npreferred_lots = 3\n"), vars(config))
check("a lot-cap warning is shown to the user in their settings", "⚠️" in onboarding.params_message(st_w) and md_ok(onboarding.params_message(st_w)))

paths.ONBOARD_FLAG = tmp / ".onboarded.json"
paths.BROKER_OK_FLAG = tmp / ".broker_verified"
sent = []
ok = lambda m: sent.append(m) or True
check("first start sends the full guide", onboarding.send_welcome_once(st, post=ok) == "sent" and len(sent) == 3)
sent.clear()
check("a restart does not repeat it", onboarding.send_welcome_once(st, post=ok) == "skipped" and sent == [])
st_changed = userprofile.build(userprofile.parse(GOOD.replace("1000000", "2000000")), vars(config), GOOD.replace("1000000", "2000000"))
check("changed settings send only a short 'settings updated' note", onboarding.send_welcome_once(st_changed, post=ok) == "updated" and len(sent) == 1 and "updated" in sent[0] and md_ok(sent[0]), str(sent))
paths.ONBOARD_FLAG = tmp / ".onboarded2.json"
sent.clear()
check("if Telegram rejects a message it is NOT marked done (retries next start)",
      onboarding.send_welcome_once(st, post=lambda m: False) == "failed" and not paths.ONBOARD_FLAG.exists())
sent.clear()
check("broker-verified notice goes out exactly once", onboarding.send_broker_verified_once(st, post=ok) is True and onboarding.send_broker_verified_once(st, post=ok) is False and len(sent) == 1 and md_ok(sent[0]))

print("\n[11] Telegram commands")
from notifications import telegram_commands as tc    # noqa: E402
cmds = {c["command"] for c in tc._BOT_COMMANDS}
check("/start and /params are in the autocomplete menu", {"start", "params"} <= cmds)
replies = []
tc._send = lambda chat, text, plain=False: replies.append(text)
tc.SETTINGS = None
tc._handle("/params", "1")
check("owner mode: /params explains there is no per-user profile", replies and "owner mode" in replies[-1].lower())
replies.clear(); tc.SETTINGS = st
tc._handle("/params", "1")
check("/params shows the user's settings", replies and "Your settings" in replies[0] and md_ok(replies[0]))
replies.clear(); tc._handle("/start", "1")
check("/start repeats the three-message guide", len(replies) == 3 and replies[0].startswith("✅ *Connected*"))
replies.clear(); tc._handle("/help", "1")
check("/help lists the new commands", "/params" in replies[0] and "/start" in replies[0])

print("\n[12] provisioning script")
import importlib                                     # noqa: E402
prov = importlib.import_module("scripts.provision_user") if (ROOT / "scripts" / "__init__.py").exists() else None
if prov is None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("provision_user", ROOT / "scripts" / "provision_user.py")
    prov = importlib.util.module_from_spec(spec); spec.loader.exec_module(prov)
base = tmp / "users"
rc = prov.main(["raj", "--capital", "1000000", "--base", str(base), "--no-seed"])
d = base / "raj"
check("creates profile, .env, logs/, data/", rc == 0 and (d / "profile.toml").exists() and (d / ".env").exists() and (d / "logs").is_dir() and (d / "data").is_dir())
check(".env is owner-only (600) and the folder is private (700)", stat.S_IMODE((d / ".env").stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700,
      f"{oct(stat.S_IMODE((d / '.env').stat().st_mode))} {oct(stat.S_IMODE(d.stat().st_mode))}")
check("the generated profile loads and gives ₹10,000 / 2 lots", userprofile.load(d / "profile.toml", vars(config)).sizing.suggested_lots == 2)
(d / ".env").write_text("KEEP=me\n")
check("re-running without --force refuses and changes nothing", prov.main(["raj", "--capital", "1000000", "--base", str(base)]) == 2 and (d / ".env").read_text() == "KEEP=me\n")
check("--force rewrites the profile but never the .env", prov.main(["raj", "--capital", "1500000", "--base", str(base), "--force", "--no-seed"]) == 0
      and (d / ".env").read_text() == "KEEP=me\n" and "1500000" in (d / "profile.toml").read_text())
check("a too-small capital creates nothing", prov.main(["tiny", "--capital", "100000", "--base", str(base)]) == 1 and not (base / "tiny").exists())
check("a bad name is refused", prov.main(["../evil", "--capital", "1000000", "--base", str(base)]) == 2)
check("above-1.5% loss limit is refused at provisioning time", prov.main(["greedy", "--capital", "1000000", "--max-loss", "50000", "--base", str(base)]) == 1)

print("\n[13] dry-run preview script")
env = {**os.environ, "ALGO_USER_DIR": str(d)}
r = subprocess.run([sys.executable, "scripts/check_profile.py"], cwd=ROOT, env=env, capture_output=True, text=True)
check("exits non-zero and names the missing secrets while the .env is empty", r.returncode == 1 and "TELEGRAM_BOT_TOKEN" in r.stdout and "missing secrets" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
(d / ".env").write_text("\n".join(f"{k}=x" for k in userprofile.ALWAYS_REQUIRED_SECRETS + userprofile.REQUIRED_SECRETS["zerodha"]) + "\n")
r = subprocess.run([sys.executable, "scripts/check_profile.py"], cwd=ROOT, env=env, capture_output=True, text=True)
check("exits 0 and previews the Telegram guide when everything is filled in", r.returncode == 0 and "profile OK" in r.stdout and "Connected" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
r = subprocess.run([sys.executable, "scripts/check_profile.py"], cwd=ROOT, env={k: v for k, v in os.environ.items() if k != "ALGO_USER_DIR"}, capture_output=True, text=True)
check("without ALGO_USER_DIR it explains how to run", r.returncode == 2 and "ALGO_USER_DIR" in r.stderr)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
