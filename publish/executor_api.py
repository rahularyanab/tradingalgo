"""
Executor API: paid subscribers' executors fetch their orders here, with a personal access key.

Runs as its OWN process (systemd: executor-api.service):
    venv/bin/python -m publish.executor_api

    GET /v1/instructions?since=<seq>   Authorization: Bearer <key>
    GET /v1/status                     Authorization: Bearer <key>

HTTPS with a self-signed certificate (EXEC_TLS_DIR/cert.pem + key.pem); executors pin its SHA-256
fingerprint, so no domain or certificate authority is needed and nobody can impersonate the server.

Who gets what:
  * Only subscribers with a key (owner command /key) and PAID access (or the owner) get orders.
    The free month covers Telegram DMs only, not auto-trading.
  * When paid access ends (lapsed or /revoke), new trades stop at once, but exits for any position
    opened while they were paid keep coming until it is flat — nobody is left with an orphan short.
  * Keys are stored only as SHA-256 hashes (in the subscriber record), never in clear.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import ssl
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import paths as _paths
from publish import executor_feed as EF

logger = logging.getLogger("executor_api")

IST = ZoneInfo("Asia/Kolkata")
SUBS: Path = _paths.LOGS_DIR / "tg_subscribers.json"
SEEN: Path = _paths.LOGS_DIR / "exec_seen.json"
TG_ENV: Path = _paths.ROOT / "tg.env"
TLS_DIR = Path(os.environ.get("EXEC_TLS_DIR", str(_paths.ROOT / "exec_tls")))
PORT = int(os.environ.get("EXEC_PORT", "8443"))
BUILD_EVERY = 2.0

_seen: dict = {}
_seen_lock = threading.Lock()


# ── keys ─────────────────────────────────────────────────────────────────────

def new_key() -> tuple[str, str]:
    """(key to hand out once, its hash to store)."""
    key = "tdx_" + secrets.token_urlsafe(32)
    return key, hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _owner_id() -> str:
    if TG_ENV.exists():
        for line in TG_ENV.read_text().splitlines():
            if line.strip().startswith("TG_OWNER_ID="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("TG_OWNER_ID", "")


def _subs() -> dict:
    try:
        return json.loads(SUBS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def who(key: str) -> Optional[tuple[str, dict]]:
    if not key:
        return None
    h = hash_key(key)
    for cid, sub in _subs().items():
        stored = sub.get("exec_key_sha256")
        if stored and secrets.compare_digest(stored, h):
            return cid, sub
    return None


# ── access ───────────────────────────────────────────────────────────────────

def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def paid_at(sub: dict, when: datetime, is_owner: bool) -> bool:
    """Was this subscriber entitled to auto-trading at `when`?"""
    if is_owner:
        return True
    paid = _dt(sub.get("paid_until"))
    start = _dt(sub.get("exec_since")) or _dt(sub.get("exec_key_created"))
    return bool(paid and when < paid and (start is None or when >= start))


def status(sub: dict, now: datetime, is_owner: bool) -> str:
    if is_owner:
        return "owner"
    return "paid" if paid_at(sub, now, False) else "inactive"


def instructions_for(sub: dict, is_owner: bool, since: int, now: datetime) -> dict:
    plan = sub.get("plan", "standard")
    out, last = [], since
    for ins in EF.read_since(since):
        last = ins["seq"]
        orders = ins["plans"].get(plan, {}).get("orders", [])
        when = _dt(ins["ts"])
        if not paid_at(sub, when, is_owner):
            opened = _dt(ins.get("opened"))
            if ins["kind"] == "OPEN" or not (opened and paid_at(sub, opened, is_owner)):
                continue                                     # not entitled, and nothing of theirs to close
            orders = [o for o in orders if o.get("closing")]  # lapsed mid-trade: exits only
            if not orders:
                continue
        out.append({k: ins[k] for k in ("seq", "src_seq", "ts", "kind", "position_id", "structure", "grade",
                                         "expiry", "reason", "sl", "flatten_by")}
                   | {"orders": orders, "open_after": ins["plans"].get(plan, {}).get("open_after", {})})
    return {"ok": True, "plan": plan, "status": status(sub, now, is_owner),
            "paid_until": sub.get("paid_until"), "server_time": now.isoformat(timespec="seconds"),
            "instructions": out, "next": last}


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ThetaDeskExec/1"

    def log_message(self, fmt, *args):                       # never log request lines (no keys in logs)
        pass

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        auth = self.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.startswith("Bearer ") else ""
        found = who(key)
        if not found:
            return self._json(401, {"ok": False, "error": "unknown key"})
        cid, sub = found
        is_owner = cid == _owner_id()
        now = datetime.now(IST)
        with _seen_lock:
            _seen[cid] = {"at": now.isoformat(timespec="seconds"), "ip": self.client_address[0]}
        if url.path == "/v1/status":
            latest = EF.read_since(0, limit=10**9)
            return self._json(200, {"ok": True, "plan": sub.get("plan", "standard"),
                                    "status": status(sub, now, is_owner), "paid_until": sub.get("paid_until"),
                                    "server_time": now.isoformat(timespec="seconds"), "flatten_by": EF.FLATTEN_BY,
                                    # a fresh executor starts here: it never replays old trades
                                    "latest_seq": latest[-1]["seq"] if latest else 0})
        if url.path == "/v1/instructions":
            try:
                since = int((parse_qs(url.query).get("since") or ["0"])[0])
            except ValueError:
                return self._json(400, {"ok": False, "error": "since must be a number"})
            return self._json(200, instructions_for(sub, is_owner, since, now))
        return self._json(404, {"ok": False, "error": "not found"})


def tls_context(tls_dir: Path = TLS_DIR) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
    return ctx


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Clients that hang up, fail the TLS handshake or probe the port: one quiet line, no traceback."""
        import sys
        logger.debug(f"connection from {client_address[0]} ended early: {sys.exc_info()[1]!r}")


def serve(port: int = PORT, tls_dir: Path = TLS_DIR, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    httpd = _Server((host, port), Handler)
    httpd.socket = tls_context(tls_dir).wrap_socket(httpd.socket, server_side=True)
    return httpd


def _background(stop: threading.Event) -> None:
    last_seen_write = 0.0
    while not stop.is_set():
        try:
            n = EF.update()
            if n:
                logger.info(f"{n} new instruction(s)")
        except Exception as e:                               # noqa: BLE001
            logger.exception(f"instruction builder: {e}")
        if time.time() - last_seen_write > 60:
            with _seen_lock:
                snapshot = dict(_seen)
            if snapshot:
                tmp = SEEN.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot))
                os.replace(tmp, SEEN)
            last_seen_write = time.time()
        stop.wait(BUILD_EVERY)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    _paths.ensure_dirs()
    try:
        _seen.update(json.loads(SEEN.read_text()))
    except (FileNotFoundError, ValueError):
        pass
    stop = threading.Event()
    threading.Thread(target=_background, args=(stop,), daemon=True).start()
    httpd = serve()
    logger.info(f"executor API on https://0.0.0.0:{PORT} (TLS {TLS_DIR})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
