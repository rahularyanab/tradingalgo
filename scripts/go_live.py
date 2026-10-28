"""
Unattended first start of a live Angel bot: verify the account, wait for the funds, then start the bot.

    ALGO_USER_DIR=/opt/algo-users/rahul-angel python3 scripts/go_live.py --service rahul-angel --test-order

Meant to be launched shortly before the market opens by a one-shot systemd timer. Everything it does is reported on the
user's own Telegram. It starts the bot ONLY when every step passed; any failure leaves the bot stopped and says why.

  1. wait until --start-at (default 09:20 IST) on a weekday
  2. log in, run the same checks as verify_angel.py (with --test-order: the 1-lot Rs 0.05 far-OTM limit buy, modified, cancelled)
     — a clean run in market hours writes the marker the bot's live gate requires
  3. wait until the account's free cash >= --min-funds (polling), no later than --give-up-at (default 13:00)
  4. enable + start trading-bot@<service> and its daily login timer, confirm it is running

The bot itself then waits for its own 10:00 entry window and obeys the same daily loss limit, pause switch and gates as always.
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def free_cash(client) -> float:
    r = client.rms() or {}
    for k in ("availablecash", "net"):
        try:
            return float(r.get(k))
        except (TypeError, ValueError):
            continue
    raise RuntimeError(f"funds not found in Angel's reply (keys: {sorted(r)[:8]})")


MIN_HEADROOM_MB = 300


def memory_headroom_mb() -> float:
    """RAM that can be used without killing anything, plus free swap (MB). The server has under 500 MB and runs several bots."""
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) / 1024
    return info.get("MemAvailable", 0) + info.get("SwapFree", 0)


def go_live(client, *, say, verify, record, start, min_funds, start_at, give_up_at, test_order=True,
            now=datetime.now, sleep=time.sleep, poll_seconds=120, retry_seconds=180, attempts=2, headroom=lambda: 10**6) -> int:
    """Returns 0 = bot started, 1 = a check failed, 2 = funds never arrived / out of time, 3 = not a trading day."""
    t = now()
    if t.weekday() >= 5:
        say("⏸ Go-live skipped: today is not a trading day. The bot was NOT started.")
        return 3
    while now().time() < start_at:
        sleep(30)
    if now().time() >= give_up_at:
        say(f"⏸ Go-live skipped: it is already past {give_up_at:%H:%M}. The bot was NOT started.")
        return 2
    say(f"🔎 *Go-live check started.* Verifying your Angel account{' with a Rs 0.05 test order' if test_order else ''}, then waiting for funds of at least ₹{min_funds:,.0f}.")

    res = None
    for n in range(1, attempts + 1):
        res = verify(client, test_order)
        if any(s == "❌" for s, _, _ in res) or not _conclusive(res):
            if n < attempts:
                say(f"⚠️ Verification attempt {n} did not pass; retrying in {retry_seconds // 60} minutes.")
                sleep(retry_seconds)
                continue
            fails = [f"• {l}: {d}" for s, l, d in res if s == "❌"] or ["• the run was not conclusive (market data or open interest missing)"]
            say("🚫 *Verification FAILED — the bot was NOT started.* No trades will be taken today.\n" + "\n".join(fails[:6]))
            return 1
        break
    record(res)
    filled = [d for s, l, d in res if l == "order FILLED"]
    say("✅ Angel verification passed" + (" (test order placed, modified and cancelled)." if test_order and not filled else ".")
        + (f"\n⚠️ The test order FILLED ({filled[0]}). Square that tiny position off in your Angel app." if filled else ""))

    if min_funds <= 0:
        try:
            cash = free_cash(client)
            say(f"💰 Free cash in your account: ₹{cash:,.0f}. No minimum is required — the bot will try; if margin is short, orders get rejected and it stops for the day after the allowed number of failed entries.")
        except Exception as e:
            cash = None
            say(f"⚠️ Could not read your funds ({e}); starting anyway as no minimum is required.")
    last_note = None
    while min_funds > 0:
        try:
            cash = free_cash(client)
        except Exception as e:
            cash = None
            say(f"⚠️ Could not read your funds: {e}")
        if cash is not None and cash >= min_funds:
            break
        if now().time() >= give_up_at:
            say(f"⏸ *Funds did not arrive by {give_up_at:%H:%M}* (free cash {'unknown' if cash is None else f'₹{cash:,.0f}'}, need ₹{min_funds:,.0f}). The bot was NOT started; no trades today.")
            return 2
        stamp = now().replace(minute=(now().minute // 30) * 30, second=0, microsecond=0)
        if cash is not None and stamp != last_note:
            say(f"⏳ Waiting for funds: ₹{cash:,.0f} free, need ₹{min_funds:,.0f}. Add funds and the bot starts by itself.")
            last_note = stamp
        sleep(poll_seconds)

    room = headroom()
    if room < MIN_HEADROOM_MB:
        say(f"🚫 *Not starting: the server is short of memory* ({room:.0f} MB usable, need {MIN_HEADROOM_MB}). Two bots scanning at once could make Linux kill one of them — "
            f"possibly your Zerodha bot. The Angel bot was NOT started. Add swap or a bigger server first.")
        return 1
    ok, why = start()
    if not ok:
        say(f"🚫 *Could not start the bot:* {why}")
        return 1
    say(f"🟢 *Your Angel bot is LIVE* — funds {'unknown' if cash is None else f'₹{cash:,.0f}'} free. It looks for trades from 10:00 AM: 1 lot, no added size, daily loss limit as set. "
        f"Send /pause to stop new trades at any time.")
    return 0


def _conclusive(res) -> bool:
    import verify_angel as V
    return V.conclusive(res)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", required=True, help="instance name, e.g. rahul-angel (systemd trading-bot@<name>)")
    ap.add_argument("--test-order", action="store_true", help="include the Rs 0.05 order lifecycle test (pre-authorised by you)")
    ap.add_argument("--min-funds", type=float, default=110_000.0)
    ap.add_argument("--start-at", default="09:20")
    ap.add_argument("--give-up-at", default="13:00")
    a = ap.parse_args(argv)

    import paths
    paths.load_instance_env()
    import requests

    def say(text):
        print(text, flush=True)
        url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "Markdown"}, timeout=15)
            if not r.ok:                                       # an unbalanced _ or * makes Telegram refuse Markdown: resend plain
                requests.post(url, json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text.replace("*", "")}, timeout=15)
        except Exception as e:
            print(f"(telegram failed: {e})", flush=True)

    import verify_angel as V
    from auth.angel_login import login
    from broker.angel import AngelClient

    def fresh_client():
        jwt = login(os.environ["ANGEL_API_KEY"], os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PASSWORD"], os.environ["ANGEL_TOTP_SECRET"])
        return AngelClient(os.environ["ANGEL_API_KEY"], jwt, paper=False)

    try:
        client = fresh_client()
    except Exception as e:
        say(f"🚫 *Angel login failed — the bot was NOT started:* {e}")
        return 1

    def verify(c, test_order):
        return V.run(c, test_order=test_order, confirm=lambda prompt: "YES", say=print)      # YES: pre-authorised by the owner for this unattended run

    def record(res):
        V.record_if_conclusive(res, os.environ["ANGEL_CLIENT_CODE"])

    def start():
        unit = f"trading-bot@{a.service}"
        try:
            for cmd in (["systemctl", "enable", "--now", unit], ["systemctl", "enable", "--now", f"trading-login@{a.service}.timer"]):
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
            time.sleep(25)
            active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
            return (active == "active", f"{unit} is '{active}' after starting — see journalctl -u {unit}")
        except Exception as e:
            return False, str(e)

    return go_live(client, say=say, verify=verify, record=record, start=start, min_funds=a.min_funds,
                   start_at=_hhmm(a.start_at), give_up_at=_hhmm(a.give_up_at), test_order=a.test_order,
                   headroom=memory_headroom_mb)


if __name__ == "__main__":
    sys.exit(main())
