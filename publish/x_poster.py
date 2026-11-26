"""
X (Twitter) poster: reads the bot's signal outbox and posts each event to X.

Runs as its OWN process (systemd: signal-publisher.service), never inside the trading bot:
    venv/bin/python -m publish.x_poster

Safe by default:
  * DRY RUN unless X_LIVE=1. Dry run writes the exact text it would post to logs/x_poster_dryrun.log.
  * First start (no cursor) begins at the END of the outbox — it never replays old trades.
  * Hourly P&L lines older than SNAPSHOT_MAX_AGE are skipped (a stale number is worse than none).
    Trade events (entry/add/exit) that arrive late are still posted, marked "(delayed)", so a
    follower never sees an entry without its exit.
  * Days with no trade and no open position post nothing (covers market holidays).
  * No links in posts: X charges far more for a post containing a URL. Put links in the bio.
  * Numbers are shown PER LOT (owner's real size is never posted).

Credentials come from X_ENV_FILE (default /opt/trading-algo/x.env, chmod 600), never from the
trading bot's .env:
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET, X_LIVE=0|1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

import paths as _paths

logger = logging.getLogger("x_poster")

POST_URL = "https://api.x.com/2/tweets"
OUTBOX: Path = _paths.SIGNAL_OUTBOX
CURSOR: Path = _paths.LOGS_DIR / "x_poster_cursor.json"
DRYRUN_LOG: Path = _paths.LOGS_DIR / "x_poster_dryrun.log"
ENV_FILE = Path(os.environ.get("X_ENV_FILE", str(_paths.ROOT / "x.env")))

POLL_SEC = 15
SNAPSHOT_MAX_AGE = 20 * 60          # skip an hourly P&L line older than this
LATE_AFTER = 10 * 60                # trade events older than this get "(delayed)"
MAX_LEN = 280
HASHTAGS = "#Nifty #OptionSelling #AlgoTrading"

_REASONS = {
    "SL_HIT": "Stop-loss hit", "PROFIT_BOOK": "Profit booked", "SIGNAL_EXIT": "Signal reversed",
    "STRANGLE_LEG_EXIT": "Leg closed on signal", "EOD": "End-of-day exit", "EOD_FLATTEN": "End-of-day exit",
    "TARGET": "Target hit", "MANUAL_EXIT": "Closed manually", "DAY_FLOOR": "Daily loss limit",
    "PROFIT_LOCK": "Trailing profit lock",
}


# ── formatting ───────────────────────────────────────────────────────────────

def _rs(x: float) -> str:
    sign = "+" if x >= 0 else "−"
    return f"{sign}₹{abs(x):,.0f}"


def _pts(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.1f} pts"


def _px(x) -> str:
    return "~market" if x is None else f"₹{x:,.2f}"


def _num(x) -> str:
    return f"{x:,.0f}"


def _time(ev: dict) -> str:
    return datetime.fromisoformat(ev["ts"]).strftime("%H:%M")


def _reason(tag: str) -> str:
    return _REASONS.get(tag, tag.replace("_", " ").capitalize())


def _leg_name(ev_or_leg: dict, inst: str) -> str:
    return f"{inst} {ev_or_leg['strike']} {ev_or_leg['leg']}"


def _fit(text: str) -> str:
    """X counts most emoji as 2; stay safely under the limit by trimming the hashtag line first."""
    def weight(s):
        return sum(2 if ord(c) > 0x2000 else 1 for c in s)
    if weight(text) <= MAX_LEN:
        return text
    lines = [l for l in text.split("\n") if not l.startswith("#")]
    text = "\n".join(lines)
    while weight(text) > MAX_LEN:
        text = text[:-2]
    return text.rstrip() + "…"


def format_event(ev: dict, book: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Text to post for one outbox event, or None to skip. `book` is the poster's running memory
    (open positions' per-lot totals); it is updated here."""
    now = now or datetime.now().astimezone()
    inst = ev.get("instrument", "NIFTY")
    age = (now - datetime.fromisoformat(ev["ts"])).total_seconds()
    late = " (delayed)" if age > LATE_AFTER else ""
    t = ev["type"]
    pid = ev.get("position_id")

    if t == "ENTRY":
        book[pid or "?"] = {"per_lot": 0.0, "start_lots": ev.get("entry_lots")}
        legs = ev["legs"]
        if ev["structure"] == "STRANGLE":
            what = " + ".join(f"{l['strike']} {l['leg']} @ {_px(l['price'])}" for l in legs)
            stop = f"spot above {_num(ev['sl_spot'])} / below {_num(ev.get('sl_spot_put') or 0)}"
            head = f"🔴 NEW TRADE{late} — {inst} strangle"
        else:
            l = legs[0]
            what = f"{l['strike']} {l['leg']} @ {_px(l['price'])}"
            above = l["leg"] == "CE"
            stop = f"spot {'above' if above else 'below'} {_num(ev['sl_spot'])}"
            head = f"🔴 NEW TRADE{late} — {inst}"
        lines = [head, f"Sold {what}", f"Spot {_num(ev['spot'])} · Stop: {stop}"]
        if ev.get("hedge"):
            h = ev["hedge"]
            lines.append(f"Hedge: bought {h['strike']} {legs[0]['leg']} @ {_px(h['price'])}")
        lines += [f"{_time(ev)} IST", HASHTAGS]
        return _fit("\n".join(lines))

    if t == "ADD":
        start = None
        if ev.get("lots_open") and ev.get("lots_added"):
            start = ev["lots_open"] - ev["lots_added"]
        legs = ", ".join(f"{l['strike']} {l['leg']} @ {_px(l['price'])}" for l in ev["legs"])
        scale = f" — size now {ev['lots_open'] / start:.1f}× start" if start else ""
        return _fit(f"➕ ADDED{late} to {inst} {legs}{scale}\nAvg entry {_px(ev.get('blended_entry'))}\n"
                    f"{_time(ev)} IST")

    if t == "EXIT":
        pos = book.get(pid) if pid else None
        # per lot from the exact ₹ P&L ÷ starting lots (the feed's own per-lot field is rounded per
        # slice, which made a trade's total drift ₹1 from the day total)
        if pos and pos.get("start_lots") and ev.get("pnl") is not None:
            per_lot = ev["pnl"] / pos["start_lots"]
        else:
            per_lot = ev.get("pnl_per_start_lot")
        if pos is not None and per_lot is not None:
            pos["per_lot"] += per_lot
        what = f"{inst} {ev['strike']} {ev['leg']}" if ev["leg"] != "BOTH" else f"{inst} strangle"
        closed = ev.get("position_closed")
        icon = "✅" if (ev.get("pnl") or 0) >= 0 else "❌"
        part = "" if closed else " (part)"
        lines = [f"{icon} EXIT{part}{late} — {what} @ {_px(ev.get('exit_price'))}",
                 f"{_reason(ev['reason'])}"]
        if ev.get("points") is not None:
            lines[-1] += f" · {_pts(ev['points'])}"
        if closed:
            total = pos["per_lot"] if pos is not None else per_lot
            if total is not None:
                lines.append(f"Trade result: {_rs(total)} per lot")
            book.pop(pid, None)
        elif closed is False and ev.get("lots_open") and ev.get("lots_closed"):
            done = ev["lots_closed"] / (ev["lots_closed"] + ev["lots_open"])
            lines.append(f"Closed {done:.0%} of the position, rest still open")
        lines.append(f"{_time(ev)} IST")
        return _fit("\n".join(lines))

    if t == "PNL_SNAPSHOT":
        if age > SNAPSHOT_MAX_AGE:
            return None
        if not ev.get("position_open") and not ev.get("trades_closed_today"):
            return None                                   # no trade today (or a holiday): stay quiet
        booked = ev.get("day_per_start_lot") or 0
        open_pl = ev.get("open_per_start_lot")
        is_close = ev.get("label") == "CLOSE"
        if ev.get("position_open") and open_pl is None:
            day = f"Booked {_rs(booked)} per lot · open P&L updating"
        else:
            day = f"Day P&L: {_rs(booked + (open_pl or 0))} per lot"
        head = f"🏁 Day closed — {inst} algo" if is_close else f"⏱ {_time(ev)} IST — {inst} algo"
        lines = [head, day]
        if ev.get("position_open") and open_pl is not None:
            lines.append(f"Booked {_rs(booked)} · Open {_rs(open_pl)}")
        n = ev.get("trades_closed_today") or 0
        lines.append(f"{n} trade{'s' if n != 1 else ''} closed today" + (" · position open" if ev.get("position_open") else ""))
        lines.append(HASHTAGS)
        return _fit("\n".join(lines))

    return None


# ── X API (OAuth 1.0a user context, stdlib signing) ──────────────────────────

def _enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~-._")


def oauth_header(method: str, url: str, creds: dict, extra_params: Optional[dict] = None,
                 nonce: Optional[str] = None, timestamp: Optional[str] = None) -> str:
    """Authorization header for an X API call. A JSON body is NOT part of the signature;
    extra_params is only for query/form params (and for the known-answer test)."""
    oauth = {
        "oauth_consumer_key": creds["api_key"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    params = {**oauth, **(extra_params or {})}
    param_str = "&".join(f"{k}={v}" for k, v in sorted((_enc(k), _enc(v)) for k, v in params.items()))
    base = "&".join([method.upper(), _enc(url), _enc(param_str)])
    key = f"{_enc(creds['api_secret'])}&{_enc(creds['access_secret'])}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(oauth.items()))


def post_to_x(text: str, creds: dict) -> tuple[bool, bool, str]:
    """Returns (ok, retryable, detail)."""
    try:
        r = requests.post(POST_URL, json={"text": text}, timeout=20,
                          headers={"Authorization": oauth_header("POST", POST_URL, creds),
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        return False, True, f"network: {e}"
    if r.status_code in (200, 201):
        return True, False, r.json().get("data", {}).get("id", "")
    retryable = r.status_code == 429 or r.status_code >= 500
    return False, retryable, f"HTTP {r.status_code}: {r.text[:300]}"


# ── the loop ─────────────────────────────────────────────────────────────────

def load_env() -> tuple[dict, bool]:
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    get = lambda k: vals.get(k) or os.environ.get(k, "")
    creds = {"api_key": get("X_API_KEY"), "api_secret": get("X_API_SECRET"),
             "access_token": get("X_ACCESS_TOKEN"), "access_secret": get("X_ACCESS_SECRET")}
    live = get("X_LIVE") == "1" and all(creds.values())
    return creds, live


def _read_cursor() -> Optional[dict]:
    try:
        return json.loads(CURSOR.read_text())
    except FileNotFoundError:
        return None


def _write_cursor(cur: dict) -> None:
    tmp = CURSOR.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur))
    os.replace(tmp, CURSOR)


def _read_events() -> list:
    if not OUTBOX.exists():
        return []
    out = []
    for line in OUTBOX.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"skipping unreadable outbox line: {line[:120]}")
    return out


def run_once(creds: dict, live: bool, poster=post_to_x, now: Optional[datetime] = None) -> int:
    """Process every new event once. Returns how many were posted (or dry-run logged)."""
    events = _read_events()
    cur = _read_cursor()
    if cur is None:                                   # first start: never replay history
        cur = {"seq": max((e.get("seq", 0) for e in events), default=0), "book": {}}
        _write_cursor(cur)
        logger.info(f"first start: cursor set to seq {cur['seq']} (history not replayed)")
        return 0
    done = 0
    for ev in sorted(events, key=lambda e: e.get("seq", 0)):
        if ev.get("seq", 0) <= cur["seq"]:
            continue
        book = json.loads(json.dumps(cur.get("book", {})))     # copy: only kept once the event is done,
        text = format_event(ev, book, now=now)                  # so a retried exit is never counted twice
        if text is not None:
            if live:
                ok, retryable, detail = poster(text, creds)
                if not ok and retryable:
                    logger.warning(f"seq {ev['seq']}: will retry — {detail}")
                    return done                         # cursor not advanced: retried next loop
                if not ok:
                    logger.error(f"seq {ev['seq']}: X refused, skipping — {detail}\n{text}")
                else:
                    logger.info(f"seq {ev['seq']}: posted {detail}")
            else:
                DRYRUN_LOG.parent.mkdir(parents=True, exist_ok=True)
                with open(DRYRUN_LOG, "a") as f:
                    f.write(f"--- seq {ev['seq']} {ev['type']} {ev['ts']}\n{text}\n\n")
                logger.info(f"seq {ev['seq']}: DRY RUN (see {DRYRUN_LOG.name})")
            done += 1
        cur["book"] = book
        cur["seq"] = ev["seq"]
        _write_cursor(cur)
    return done


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    _paths.ensure_dirs()
    creds, live = load_env()
    logger.info(f"X poster starting — {'LIVE' if live else 'DRY RUN'}; outbox {OUTBOX}")
    backoff = POLL_SEC
    while True:
        try:
            new_creds, new_live = load_env()                  # pick up key changes without a restart
            if new_live != live:
                logger.info(f"mode changed: {'LIVE' if new_live else 'DRY RUN'}")
            creds, live = new_creds, new_live
            run_once(creds, live)
            backoff = POLL_SEC
        except Exception as e:                                # noqa: BLE001 — keep the loop alive
            logger.exception(f"loop error: {e}")
            backoff = min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
