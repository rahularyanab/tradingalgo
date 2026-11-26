"""
The subscriber fleet: one small DigitalOcean droplet per auto-trading subscriber, in Theta Desk's account.

Runs as its OWN process (systemd: fleet.service):   venv/bin/python -m publish.fleet

Life of a subscriber's server (state in logs/fleet.json, one record per Telegram chat id):

  requested ──► creating ──► installing ──► awaiting_broker ──► paper ──► live
   (/autotrade)   droplet      code + key,      guide + setup      daily      owner
                  created      PAPER mode       link sent          summary    approved
                                                                     │
  any of awaiting_broker / paper / live, once paid access has lapsed and the account is flat
  ──► off (service stopped, droplet powered off) ──► deleted after OFF_DAYS, unless paid again

* The subscriber's "static IP" for their broker is simply the droplet's own public IP: it is the address
  the broker sees for outgoing orders and it never changes while the droplet exists.
* Broker logins are never stored on the main server: the setup page (publish/setup_web.py) passes them
  straight to the subscriber's droplet over SSH.
* Each droplet only accepts SSH from the main server (ufw, set up by deploy/executor/install_remote.sh).
* A droplet budget (TG fleet setting FLEET_MAX, default 10) stops runaway costs.

Settings in fleet.env (chmod 600, gitignored):  DO_TOKEN, FLEET_MAX, SETUP_BASE_URL (e.g. https://setup.thetadesk.in)
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

import paths as _paths

logger = logging.getLogger("fleet")
IST = ZoneInfo("Asia/Kolkata")

STATE: Path = _paths.LOGS_DIR / "fleet.json"
LOCK: Path = _paths.LOGS_DIR / "fleet.lock"
SUBS: Path = _paths.LOGS_DIR / "tg_subscribers.json"
ENV_FILE: Path = _paths.ROOT / "fleet.env"
TG_ENV: Path = _paths.ROOT / "tg.env"
SSH_KEY: Path = Path(os.environ.get("FLEET_SSH_KEY", "/root/.ssh/thetadesk_fleet"))
BUNDLE_SRC = _paths.ROOT / "deploy" / "executor"

MAIN_IP = "159.223.87.53"
API_URL = f"https://{MAIN_IP}:8443"
API_PIN = "d05e6407b0b182b7f7272029769c00b7634ac2db301f3d4b11f680e84a12bc6e"
REGION, SIZE, IMAGE = "blr1", "s-1vcpu-512mb-10gb", "ubuntu-24-04-x64"
OFF_DAYS = 7
SETUP_DAYS = 3
SUMMARY_AT = dtime(15, 50)
REMOTE_CODE, REMOTE_HOME = "/opt/thetadesk-executor", "/opt/executor"
ACTIVE = ("awaiting_broker", "paper", "live")


def now_ist() -> datetime:
    return datetime.now(IST)


def _read_env(path: Path) -> dict:
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def settings() -> dict:
    v = _read_env(ENV_FILE)
    return {"do_token": v.get("DO_TOKEN", ""), "max": int(v.get("FLEET_MAX") or 10),
            "setup_base": (v.get("SETUP_BASE_URL") or "").rstrip("/")}


# ── state (shared with the bot and the setup page; always under the file lock) ──

@contextlib.contextmanager
def locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save(fleet: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(fleet, indent=1))
    os.replace(tmp, STATE)


def _subs() -> dict:
    try:
        return json.loads(SUBS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_subs(subs: dict) -> None:
    tmp = SUBS.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs))
    os.replace(tmp, SUBS)


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def request(cid: str, broker: str, now: Optional[datetime] = None) -> dict:
    """Owner asked for an auto-trading server for this subscriber (/autotrade). Idempotent."""
    now = now or now_ist()
    with locked():
        fleet = load()
        rec = fleet.get(cid)
        if rec and rec["state"] not in ("deleted", "failed"):
            rec["broker"] = broker
            save(fleet)
            return rec
        rec = fleet[cid] = {"state": "requested", "broker": broker, "requested": now.isoformat(timespec="seconds"),
                            "history": [], "attempts": 0}
        save(fleet)
        return rec


def set_state(rec: dict, state: str, now: datetime, note: str = "") -> None:
    rec["history"] = (rec.get("history") or [])[-30:] + [{"at": now.isoformat(timespec="seconds"), "state": state, "note": note}]
    rec["state"] = state
    rec.pop("error", None)
    rec["attempts"] = 0


def new_setup_link(rec: dict, now: datetime, base: str) -> str:
    token = secrets.token_urlsafe(24)
    rec["setup_hash"] = hash_token(token)
    rec["setup_expires"] = (now + timedelta(days=SETUP_DAYS)).isoformat(timespec="seconds")
    rec["setup_tries"] = 0
    return f"{base}/s/{token}" if base else f"(setup page not configured yet) token {token}"


# ── DigitalOcean API ─────────────────────────────────────────────────────────

class DOError(Exception):
    pass


class DO:
    BASE = "https://api.digitalocean.com/v2"

    def __init__(self, token: str, http=None):
        self.token, self.http = token, http or requests

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        r = self.http.request(method, self.BASE + path, json=body, timeout=30,
                              headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            raise DOError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}

    def ensure_ssh_key(self, pubkey: str) -> str:
        keys = self._req("GET", "/account/keys?per_page=200").get("ssh_keys", [])
        for k in keys:
            if k.get("public_key", "").split()[:2] == pubkey.split()[:2]:
                return k["fingerprint"]
        return self._req("POST", "/account/keys", {"name": "thetadesk-fleet", "public_key": pubkey})["ssh_key"]["fingerprint"]

    def create_droplet(self, name: str, ssh_fp: str) -> int:
        body = {"name": name, "region": REGION, "size": SIZE, "image": IMAGE, "ssh_keys": [ssh_fp],
                "backups": False, "ipv6": False, "monitoring": False, "tags": ["thetadesk-executor"]}
        return int(self._req("POST", "/droplets", body)["droplet"]["id"])

    def droplet(self, did: int) -> dict:
        d = self._req("GET", f"/droplets/{did}")["droplet"]
        ip = next((n["ip_address"] for n in d.get("networks", {}).get("v4", []) if n.get("type") == "public"), None)
        return {"status": d.get("status"), "ip": ip}

    def count(self) -> int:
        return len(self._req("GET", "/droplets?tag_name=thetadesk-executor&per_page=200").get("droplets", []))

    def action(self, did: int, kind: str) -> None:
        self._req("POST", f"/droplets/{did}/actions", {"type": kind})

    def delete(self, did: int) -> None:
        self._req("DELETE", f"/droplets/{did}")


# ── SSH to a droplet ─────────────────────────────────────────────────────────

class Ssh:
    def __init__(self, key: Optional[Path] = None):
        self.key = key or SSH_KEY

    def _base(self) -> list:
        return ["-i", str(self.key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", "-o", f"UserKnownHostsFile={self.key.parent / 'fleet_known_hosts'}"]

    def run(self, ip: str, cmd: str, stdin: Optional[str] = None, timeout: int = 600) -> tuple[int, str]:
        p = subprocess.run(["ssh", *self._base(), f"root@{ip}", cmd], input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-4000:]

    def upload(self, ip: str, local_dir: Path, remote_dir: str) -> tuple[int, str]:
        p = subprocess.run(["rsync", "-a", "--delete", "--exclude", "venv", "-e", "ssh " + " ".join(self._base()),
                            f"{local_dir}/", f"root@{ip}:{remote_dir}/"], capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout + p.stderr)[-2000:]


def ssh_factory():
    """The SSH runner the bot and setup page use (tests swap it for a fake)."""
    return Ssh()


def ensure_fleet_key(key: Optional[Path] = None) -> str:
    key = key or SSH_KEY
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "thetadesk-fleet", "-f", str(key)],
                       check=True, capture_output=True)
    return Path(str(key) + ".pub").read_text().strip()


def build_bundle(root: Path = _paths.ROOT) -> Path:
    """The files a droplet needs: the executor, the Angel adapter, shims — never any strategy code."""
    out = Path(tempfile.mkdtemp(prefix="td-bundle-"))
    shutil.copytree(root / "executor", out / "executor", ignore=shutil.ignore_patterns("__pycache__", "*.example"))
    (out / "broker").mkdir()
    for f in ("angel.py", "angel_instruments.py", "angel_paper.py"):
        shutil.copy(root / "broker" / f, out / "broker" / f)
    src = root / "deploy" / "executor"
    shutil.copy(src / "broker_init.py", out / "broker" / "__init__.py")
    for f in ("paths.py", "requirements.txt", "executor.service", "install_remote.sh"):
        shutil.copy(src / f, out / f)
    bad = [str(p) for p in out.rglob("*.py")
           if re.search(r"^\s*(from|import)\s+(strategy|signals|publish|main|config|notifications|data|execution)\b",
                        p.read_text(), re.M)]
    if bad:
        shutil.rmtree(out)
        raise RuntimeError(f"bundle would import strategy-side code: {bad}")
    return out


def env_text(key: str, broker: str = "paper", mode: str = "paper", creds: Optional[dict] = None) -> str:
    lines = [f"EXEC_API_URL={API_URL}", f"EXEC_API_PIN={API_PIN}", f"EXEC_API_KEY={key}",
             f"BROKER={broker}", f"MODE={mode}"]
    lines += [f"{k}={v}" for k, v in (creds or {}).items()]
    return "\n".join(lines) + "\n"


WRITE_ENV = (f"umask 077 && cat > {REMOTE_HOME}/executor.env.new && chmod 600 {REMOTE_HOME}/executor.env.new "
             f"&& mv {REMOTE_HOME}/executor.env.new {REMOTE_HOME}/executor.env")
CHECK = f"cd {REMOTE_CODE} && EXECUTOR_HOME={REMOTE_HOME} venv/bin/python -m executor check"
SET_MODE = (f"sed -i -e 's/^MODE=.*/MODE={{mode}}/' -e '/^MAX_LOTS_PER_LEG=/d' {REMOTE_HOME}/executor.env "
            f"&& {{cap}} systemctl restart thetadesk-executor && grep -c '^MODE={{mode}}$' {REMOTE_HOME}/executor.env")
POSITIONS = (f"python3 -c \"import json; print(len(json.load(open('{REMOTE_HOME}/executor_state.json'))"
             f".get('positions', {{}})))\" 2>/dev/null || echo 0")


# ── messages to the subscriber ───────────────────────────────────────────────

def guide_text(broker: str, ip: str) -> str:
    if broker == "angel":
        return (f"🛠 <b>Your trading server is ready.</b>\nIts fixed IP address is <code>{ip}</code> — you'll need it below.\n\n"
                "<b>Step 1 of 3 · Angel One API app</b> (about 5 minutes, on a computer)\n"
                "1. Open <b>smartapi.angelbroking.com</b> and sign in with your Angel One client code.\n"
                "2. Choose <b>Create an App</b> → type <b>Trading APIs</b>.\n"
                "3. App name: <b>ThetaDesk</b>. Redirect URL: <code>https://127.0.0.1</code>\n"
                f"4. Where it asks for your <b>static / primary IP</b>, enter <code>{ip}</code>\n"
                "5. Save. Copy the <b>API key</b> it shows.\n\n"
                "<b>Step 2 of 3 · Your TOTP secret</b>\n"
                "1. On smartapi.angelbroking.com open <b>Enable TOTP</b>, enter client code + PIN + the OTP sent to you.\n"
                "2. You'll see a QR code and a <b>text key</b> under it — copy that key (letters and numbers).\n"
                "3. Scan the QR in your authenticator app too, so your login codes keep working.\n\n"
                "<b>Step 3 of 3</b> · Tap <b>Open my setup page</b> below and fill in: client code, PIN, TOTP key, API key.\n\n"
                "Stuck anywhere? Tap <b>Book a setup call</b> — we'll do it together in 15 minutes.")
    return (f"🛠 <b>Your trading server is ready.</b>\nIts fixed IP address is <code>{ip}</code> — you'll need it below.\n\n"
            "<b>Step 1 of 3 · Zerodha API app</b> (about 5 minutes, on a computer)\n"
            "1. Open <b>developers.kite.trade</b> and sign in with your Zerodha user ID.\n"
            "2. Click <b>Create new app</b>.\n"
            "3. App name: <b>ThetaDesk</b>. Redirect URL: <code>https://127.0.0.1</code>\n"
            f"4. In the <b>IP whitelist / static IP</b> box, enter <code>{ip}</code>\n"
            "5. Save. Keep the page open — it shows your <b>API key</b> and <b>API secret</b>.\n\n"
            "<b>Step 2 of 3 · Your TOTP secret</b>\n"
            "1. On kite.zerodha.com go to <b>My profile → Password & Security</b>.\n"
            "2. Turn <b>External 2FA TOTP</b> off and on again.\n"
            "3. Under the QR code, tap <b>Can't scan? Copy the key</b> and copy that text key.\n"
            "4. Scan the QR in your authenticator app too, so your login codes keep working.\n\n"
            "<b>Step 3 of 3</b> · Tap <b>Open my setup page</b> below and fill in: user ID, password, TOTP key, API key, API secret.\n\n"
            "Stuck anywhere? Tap <b>Book a setup call</b> — we'll do it together in 15 minutes.")


def guide_buttons(link: str) -> dict:
    rows = []
    if link.startswith("https://"):
        rows.append([{"text": "🔐 Open my setup page", "url": link}])
    rows.append([{"text": "📞 Book a setup call", "callback_data": "fleet:call"}])
    return {"inline_keyboard": rows}


def summarise_log(log: str, day: str) -> tuple[list, Optional[float]]:
    """(today's actions, paper P&L or None) from an executor.log tail."""
    actions, pnl, seen_paper = [], 0.0, False
    for line in log.splitlines():
        if not line.startswith(day):
            continue
        msg = line.split("  ", 2)[-1].strip()
        m = re.search(r"\[PAPER\] (BUY|SELL) \S+ x(\d+) @ ([\d.]+)", msg)
        if m:
            seen_paper = True
            q, px = int(m.group(2)), float(m.group(3))
            pnl += q * px if m.group(1) == "SELL" else -q * px
            continue
        if re.match(r"#\d+ |.*(flat|flatten|local stop|skipped)", msg):
            actions.append(msg)
    return actions, (round(pnl) if seen_paper else None)


# ── the worker ───────────────────────────────────────────────────────────────

class Fleet:
    def __init__(self, do: Optional[DO], ssh, notify: Callable, cfg: dict, clock: Callable[[], datetime] = now_ist,
                 bundle: Callable[[], Path] = build_bundle):
        self.do, self.ssh, self.notify, self.cfg, self.clock, self.bundle = do, ssh, notify, cfg, clock, bundle

    def owner(self, text: str) -> None:
        owner = _read_env(TG_ENV).get("TG_OWNER_ID")
        if owner:
            self.notify(owner, text, None)

    def tick(self) -> None:
        now = self.clock()
        with locked():
            cids = list(load().keys())
        for cid in cids:
            with locked():
                fleet = load()
                rec = fleet.get(cid)
                if not rec or rec["state"] in ("deleted", "failed"):
                    continue
                if rec.get("retry_at") and datetime.fromisoformat(rec["retry_at"]) > now:
                    continue
                snapshot = json.loads(json.dumps(rec))
            try:
                self.step(cid, snapshot, now)
            except Exception as e:                           # noqa: BLE001 — one server's trouble never stops the others
                snapshot["attempts"] = snapshot.get("attempts", 0) + 1
                snapshot["error"] = str(e)[:300]
                snapshot["retry_at"] = (now + timedelta(minutes=min(30, 2 ** snapshot["attempts"]))).isoformat(timespec="seconds")
                logger.warning(f"{cid} {snapshot['state']}: {e}")
                if snapshot["attempts"] in (3, 8):
                    self.owner(f"⚠️ Fleet: {cid} stuck at <b>{snapshot['state']}</b> ({snapshot['attempts']} tries): {str(e)[:200]}")
            with locked():
                fleet = load()
                if cid in fleet and fleet[cid].get("state") == rec["state"]:
                    fleet[cid] = {**fleet[cid], **{k: v for k, v in snapshot.items()}}
                elif cid in fleet:
                    # the setup page or the bot moved it on meanwhile: keep theirs, add our technical fields only
                    for k in ("droplet_id", "ip", "error", "retry_at", "attempts", "last_summary", "off_at"):
                        if k in snapshot:
                            fleet[cid][k] = snapshot[k]
                save(fleet)

    def step(self, cid: str, rec: dict, now: datetime) -> None:
        import publish.executor_api as EA
        subs = _subs()
        sub = subs.get(cid, {})
        paid = EA.paid_at(sub, now, False)
        st = rec["state"]

        if st == "requested":
            if not self.do:
                raise RuntimeError("no DigitalOcean token in fleet.env yet")
            if self.do.count() >= self.cfg.get("max", 10):
                raise RuntimeError(f"droplet budget of {self.cfg.get('max', 10)} reached (FLEET_MAX)")
            fp = self.do.ensure_ssh_key(ensure_fleet_key())
            rec["droplet_id"] = self.do.create_droplet(f"td-exec-{re.sub(r'[^a-z0-9-]', '', cid.lower())[:20]}", fp)
            set_state(rec, "creating", now, f"droplet {rec['droplet_id']}")
            return

        if st == "creating":
            d = self.do.droplet(rec["droplet_id"])
            if d["status"] != "active" or not d["ip"]:
                raise RuntimeError(f"droplet not ready yet ({d['status']})")
            rec["ip"] = d["ip"]
            set_state(rec, "installing", now, f"ip {d['ip']}")
            return

        if st == "installing":
            rc, out = self.ssh.run(rec["ip"], "true", timeout=30)
            if rc != 0:
                raise RuntimeError("SSH not reachable yet")
            bundle = self.bundle()
            try:
                rc, out = self.ssh.upload(rec["ip"], bundle, REMOTE_CODE)
            finally:
                shutil.rmtree(bundle, ignore_errors=True)
            if rc != 0:
                raise RuntimeError(f"upload failed: {out[-200:]}")
            rc, out = self.ssh.run(rec["ip"], f"bash {REMOTE_CODE}/install_remote.sh {MAIN_IP}")
            if rc != 0 or "INSTALLED" not in out:
                raise RuntimeError(f"install failed: {out[-300:]}")
            key, digest = EA.new_key()
            rc, out = self.ssh.run(rec["ip"], WRITE_ENV, stdin=env_text(key), timeout=60)
            if rc != 0:
                raise RuntimeError(f"settings not written: {out[-200:]}")
            subs = _subs()
            if cid in subs:
                subs[cid].update(exec_key_sha256=digest, exec_key_created=now.isoformat(timespec="seconds"),
                                 exec_since=now.isoformat(timespec="seconds"))
                _save_subs(subs)
            self.ssh.run(rec["ip"], "systemctl restart thetadesk-executor", timeout=60)
            link = new_setup_link(rec, now, self.cfg.get("setup_base", ""))
            set_state(rec, "awaiting_broker", now)
            self.notify(cid, guide_text(rec["broker"], rec["ip"]), guide_buttons(link))
            self.owner(f"🖥 Fleet: server for {sub.get('name') or cid} is ready ({rec['ip']}); guide sent.")
            return

        if st in ACTIVE and not paid:
            n = 0
            if st == "live" or st == "paper":
                rc, out = self.ssh.run(rec["ip"], POSITIONS, timeout=60)
                n = int(out.strip().split()[-1]) if rc == 0 and out.strip() else 1
            if n == 0 and (st == "awaiting_broker" or now.time() >= dtime(15, 30) or now.weekday() >= 5):
                self.ssh.run(rec["ip"], "systemctl stop thetadesk-executor", timeout=60)
                self.do.action(rec["droplet_id"], "power_off")
                rec["off_at"] = now.isoformat(timespec="seconds")
                rec["was"] = st
                set_state(rec, "off", now, "access lapsed")
                self.owner(f"⏸ Fleet: {sub.get('name') or cid}'s server powered off (access lapsed). "
                           f"Deleted in {OFF_DAYS} days unless they pay again.")
            return

        if st == "off":
            if paid:
                self.do.action(rec["droplet_id"], "power_on")
                set_state(rec, "resuming", now)
            elif now - datetime.fromisoformat(rec["off_at"]) >= timedelta(days=OFF_DAYS):
                self.do.delete(rec["droplet_id"])
                set_state(rec, "deleted", now, f"after {OFF_DAYS} days off")
                self.owner(f"🗑 Fleet: {sub.get('name') or cid}'s server deleted.")
            return

        if st == "resuming":
            rc, _ = self.ssh.run(rec["ip"], "systemctl start thetadesk-executor", timeout=60)
            if rc != 0:
                raise RuntimeError("server not back yet")
            set_state(rec, rec.get("was") or "paper", now, "access renewed")
            return

        if st in ("paper", "live") and now.time() >= SUMMARY_AT and now.weekday() < 5 \
                and rec.get("last_summary") != now.date().isoformat():
            rc, log = self.ssh.run(rec["ip"], f"tail -n 600 {REMOTE_HOME}/executor.log", timeout=60)
            actions, pnl = summarise_log(log if rc == 0 else "", now.date().isoformat())
            rec["last_summary"] = now.date().isoformat()
            label = "Paper day" if st == "paper" else "Your day"
            if not actions:
                text = f"📋 <b>{label}</b> — no trades today."
            else:
                text = (f"📋 <b>{label}</b> — what your server did today:\n" + "\n".join(f"• {a[:120]}" for a in actions[-12:])
                        + (f"\n\nPaper P&L: <b>{'+' if pnl >= 0 else '−'}₹{abs(pnl):,}</b> (simulated, before charges)" if pnl is not None else ""))
            markup = ({"inline_keyboard": [[{"text": "🚀 I'm ready to go live (1 lot)", "callback_data": "fleet:live"}]]}
                      if st == "paper" else None)
            self.notify(cid, text, markup)


# ── actions used by the bot and the setup page ───────────────────────────────

def set_mode(cid: str, ssh, mode: str, cap_lots: Optional[int], now: Optional[datetime] = None) -> str:
    """Switch a subscriber's server between paper and live, optionally capping every leg at `cap_lots`
    (going live starts at 1 lot; /fullsize lifts the cap). Returns 'ok' or a reason."""
    now = now or now_ist()
    with locked():
        rec = load().get(cid)
        if not rec or rec["state"] not in ("paper", "live"):
            return f"their server is {'not set up' if not rec else rec['state']}"
        ip = rec["ip"]
    cap = f"echo MAX_LOTS_PER_LEG={int(cap_lots)} >> {REMOTE_HOME}/executor.env &&" if cap_lots else ""
    rc, out = ssh.run(ip, SET_MODE.format(mode=mode, cap=cap), timeout=60)
    if rc != 0 or not out.strip().endswith("1"):
        return f"switch failed: {out[-200:]}"
    with locked():
        fleet = load()
        rec = fleet[cid]
        rec["cap_lots"] = cap_lots
        if rec["state"] != mode:
            set_state(rec, mode, now, f"{mode}" + (f", capped at {cap_lots} lot(s)" if cap_lots else ", full size"))
        save(fleet)
    return "ok"


def find_by_token(token: str, now: Optional[datetime] = None) -> Optional[str]:
    now = now or now_ist()
    h = hash_token(token)
    for cid, rec in load().items():
        if rec.get("setup_hash") and secrets.compare_digest(rec["setup_hash"], h) \
                and datetime.fromisoformat(rec["setup_expires"]) > now and rec["state"] == "awaiting_broker":
            return cid
    return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    _paths.ensure_dirs()
    from publish.telegram_live import TG, load_settings as tg_settings
    tg = TG(tg_settings()["token"])

    def notify(chat_id, text, markup):
        r = tg.send(str(chat_id), text, **({"reply_markup": markup} if markup else {}))
        if not r.get("ok"):
            logger.warning(f"notify {chat_id}: {r.get('description')}")

    logger.info("fleet worker up")
    while True:
        cfg = settings()
        fleet = Fleet(DO(cfg["do_token"]) if cfg["do_token"] else None, Ssh(), notify, cfg)
        try:
            fleet.tick()
        except Exception as e:                               # noqa: BLE001
            logger.exception(f"tick: {e}")
        time.sleep(15)


if __name__ == "__main__":
    main()
