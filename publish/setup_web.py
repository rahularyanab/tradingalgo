"""
The subscriber setup page: a one-time private link where a (non-technical) subscriber types their broker
login, which goes STRAIGHT to their own trading server — it is never saved or logged on this machine.

Runs as its OWN process (systemd: setup-web.service) on 127.0.0.1:8090, behind Caddy which provides the
real domain + HTTPS (e.g. https://setup.thetadesk.in).   venv/bin/python -m publish.setup_web

    GET  /s/<token>   the form for their broker (Zerodha or Angel One), with "where do I find this?" help
    POST /s/<token>   checks the format, writes the details to their droplet over SSH, runs the executor's
                      own check there, and shows ✅ or a plain-language reason. On success the bot tells them
                      paper trading starts next market day.
Links expire after 3 days and allow 10 attempts; request bodies are never logged.
"""
from __future__ import annotations

import html
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs

from publish import fleet as FL

logger = logging.getLogger("setup_web")
MAX_TRIES = 10
BASE32 = re.compile(r"^[A-Z2-7]{16,64}$")

FIELDS = {
    "zerodha": [
        ("ZERODHA_USER_ID", "Zerodha user ID", "text", "The ID you log in to Kite with, e.g. AB1234.", r"^[A-Z]{2,3}\d{3,6}$"),
        ("ZERODHA_PASSWORD", "Zerodha password", "password", "Your normal Kite login password.", r"^.{6,}$"),
        ("ZERODHA_TOTP_SECRET", "TOTP key", "text", "The text key under the QR code (Step 2 in the bot) — letters and numbers, not the 6-digit code.", "base32"),
        ("KITE_API_KEY", "API key", "text", "Shown on your app page at developers.kite.trade.", r"^[a-z0-9]{8,40}$"),
        ("KITE_API_SECRET", "API secret", "password", "On the same app page — click 'Show API secret'.", r"^[a-z0-9]{16,64}$"),
    ],
    "angel": [
        ("ANGEL_CLIENT_CODE", "Angel One client code", "text", "The code you log in with, e.g. R123456.", r"^[A-Z0-9]{4,12}$"),
        ("ANGEL_PASSWORD", "PIN", "password", "Your 4-digit Angel One login PIN.", r"^\d{4,6}$"),
        ("ANGEL_TOTP_SECRET", "TOTP key", "text", "The text key under the QR code on smartapi.angelbroking.com/enable-totp.", "base32"),
        ("ANGEL_API_KEY", "API key", "text", "Shown on your app at smartapi.angelbroking.com.", r"^[A-Za-z0-9]{6,40}$"),
    ],
}

STYLE = """<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;margin:24px auto;padding:0 16px;color:#1a1a1a}
h1{font-size:22px}label{display:block;font-weight:600;margin-top:18px}input{width:100%;padding:12px;font-size:16px;margin-top:6px;
box-sizing:border-box;border:1px solid #bbb;border-radius:8px}small{color:#555;display:block;margin-top:4px}button{margin-top:24px;
width:100%;padding:14px;font-size:17px;border:0;border-radius:8px;background:#1565c0;color:#fff}.ok{background:#e8f5e9;padding:14px;
border-radius:8px}.bad{background:#fdecea;padding:14px;border-radius:8px}.note{color:#555;font-size:14px;margin-top:20px}</style>"""


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{html.escape(title)}</title>{STYLE}</head><body>{body}</body></html>").encode()


def form(broker: str, errors: Optional[list] = None, values: Optional[dict] = None) -> bytes:
    values = values or {}
    name = "Zerodha" if broker == "zerodha" else "Angel One"
    rows = []
    for key, label, kind, help_, _ in FIELDS[broker]:
        v = "" if kind == "password" else html.escape(values.get(key, ""))
        rows.append(f"<label for={key}>{html.escape(label)}</label><input id={key} name={key} type={kind} value='{v}' "
                    f"autocomplete=off autocapitalize=none spellcheck=false required><small>{html.escape(help_)}</small>")
    err = ("<div class=bad><b>Please check:</b><ul>" + "".join(f"<li>{html.escape(e)}</li>" for e in errors) + "</ul></div>") if errors else ""
    return page(f"Connect {name}", f"<h1>Connect your {name} account</h1>{err}"
                f"<p>These details go straight to <b>your own trading server</b> and are used only to place your plan's trades. "
                f"We never store them anywhere else.</p><form method=post>{''.join(rows)}<button type=submit>Connect</button></form>"
                f"<p class=note>Stuck? Go back to the Telegram bot and tap <b>Book a setup call</b>.</p>")


def validate(broker: str, data: dict) -> tuple[dict, list]:
    clean, errors = {}, []
    for key, label, _, _, rule in FIELDS[broker]:
        v = (data.get(key) or "").strip()
        if key.endswith("TOTP_SECRET"):
            v = v.replace(" ", "").upper()
        elif key in ("ZERODHA_USER_ID", "ANGEL_CLIENT_CODE"):
            v = v.upper()
        ok = bool(BASE32.match(v)) if rule == "base32" else bool(re.match(rule, v))
        if not ok:
            errors.append(f"{label} doesn't look right." + (" It should be the text key (letters A–Z and digits 2–7), not the 6-digit code."
                                                            if rule == "base32" else ""))
        clean[key] = v
    return clean, errors


def friendly(check_output: str) -> list:
    """Turn the executor's check output into plain language."""
    out = []
    for line in check_output.splitlines():
        if not line.startswith("✗"):
            continue
        l = line.lower()
        if "totp" in l:
            out.append("The TOTP key was not accepted. Copy the text key under the QR code again (Step 2).")
        elif "login failed" in l or "password" in l or "invalid" in l:
            out.append("The broker rejected the login. Check your user ID / client code and password / PIN.")
        elif "request token" in l or "redirect" in l:
            out.append("Your Zerodha API app needs the redirect URL https://127.0.0.1 (Step 1, point 3).")
        elif "ip" in l:
            out.append("The broker doesn't allow your server's IP yet. Add the IP from the bot to your API app (Step 1, point 4).")
        else:
            out.append("Something didn't connect: " + line.lstrip("✗ ").strip()[:160])
    return out or ["The connection check didn't pass. Tap 'Book a setup call' in the bot and we'll sort it out."]


def submit(cid: str, broker: str, clean: dict, ssh, notify: Callable, now=None) -> tuple[bool, list]:
    """Write the details to the subscriber's droplet, run the check there, update state. (ok, messages)"""
    now = now or FL.now_ist()
    with FL.locked():
        rec = FL.load().get(cid)
        if not rec or rec["state"] != "awaiting_broker":
            return False, ["This link has already been used or has expired. Ask in the bot for a new one."]
        ip = rec["ip"]
    rc, existing = ssh.run(ip, f"grep '^EXEC_API_KEY=' {FL.REMOTE_HOME}/executor.env", timeout=30)
    key = existing.strip().split("=", 1)[-1] if rc == 0 else ""
    if not key.startswith("tdx_"):
        return False, ["Your trading server isn't ready yet. Please try again in a few minutes."]
    rc, out = ssh.run(ip, FL.WRITE_ENV, stdin=FL.env_text(key, broker=broker, mode="paper", creds=clean), timeout=60)
    if rc != 0:
        return False, ["Couldn't reach your trading server. Please try again in a few minutes."]
    rc, out = ssh.run(ip, FL.CHECK, timeout=120)
    if rc != 0:
        with FL.locked():
            fleet = FL.load()
            fleet[cid]["setup_tries"] = fleet[cid].get("setup_tries", 0) + 1
            FL.save(fleet)
        return False, friendly(out)
    ssh.run(ip, "systemctl restart thetadesk-executor", timeout=60)
    with FL.locked():
        fleet = FL.load()
        rec = fleet[cid]
        rec.pop("setup_hash", None)
        FL.set_state(rec, "paper", now, "broker connected")
        FL.save(fleet)
    notify(cid, ("✅ <b>Your broker is connected.</b>\nYour server starts in <b>paper mode</b> from the next market day: it follows "
                 "every trade without placing real orders, and you'll get a summary each evening.\n"
                 "When you're happy, tap <b>I'm ready to go live</b> under a summary."), None)
    return True, []


class Handler(BaseHTTPRequestHandler):
    server_version = "ThetaDeskSetup/1"
    ssh = None
    notify = staticmethod(lambda *a: None)

    def log_message(self, fmt, *args):                       # never log paths (they hold the token) or bodies
        pass

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _cid(self) -> Optional[tuple[str, str]]:
        m = re.fullmatch(r"/s/([A-Za-z0-9_-]{20,64})", self.path.split("?")[0])
        if not m:
            return None
        cid = FL.find_by_token(m.group(1))
        if not cid:
            return None
        rec = FL.load()[cid]
        if rec.get("setup_tries", 0) >= MAX_TRIES:
            return None
        return cid, rec["broker"]

    def do_GET(self):
        found = self._cid()
        if not found:
            return self._send(404, page("Link expired", "<h1>This link has expired</h1><p>Ask in the Telegram bot for a new one.</p>"))
        self._send(200, form(found[1]))

    def do_POST(self):
        found = self._cid()
        if not found:
            return self._send(404, page("Link expired", "<h1>This link has expired</h1><p>Ask in the Telegram bot for a new one.</p>"))
        cid, broker = found
        n = int(self.headers.get("Content-Length") or 0)
        data = {k: v[0] for k, v in parse_qs(self.rfile.read(min(n, 8192)).decode(errors="replace")).items()}
        clean, errors = validate(broker, data)
        if errors:
            return self._send(200, form(broker, errors, clean))
        ok, msgs = submit(cid, broker, clean, self.ssh, self.notify)
        if ok:
            return self._send(200, page("Connected", "<h1>✅ Connected</h1><div class=ok>Your broker account is connected to your "
                                        "trading server. You can close this page — the Telegram bot will keep you posted.</div>"))
        self._send(200, form(broker, msgs, clean))


def serve(port: int = 8090, ssh=None, notify=None) -> ThreadingHTTPServer:
    Handler.ssh = ssh or FL.Ssh()
    if notify:
        Handler.notify = staticmethod(notify)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    from publish.telegram_live import TG, load_settings as tg_settings
    tg = TG(tg_settings()["token"])
    httpd = serve(notify=lambda cid, text, markup: tg.send(str(cid), text, **({"reply_markup": markup} if markup else {})))
    logger.info("setup page on http://127.0.0.1:8090 (public via Caddy)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
