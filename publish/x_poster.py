"""
X (Twitter) poster: reads the bot's signal outbox and posts to X on a fixed daily plan.

Runs as its OWN process (systemd: signal-publisher.service), never inside the trading bot:
    venv/bin/python -m publish.x_poster

Daily plan (market days only — weekdays not listed in publish/nse_holidays.txt), at most
DAILY_CAP posts:
    09:00        greeting: follow the account to get live trades
    09:05        one trading-wisdom line, rotating through publish/quotes.py
    09:15        first scan result (bias, levels) + "trades start at 10:00 sharp"
    ~10:00 on    a post when a position OPENS and when it fully CLOSES (with its per-lot result)
    hourly       P&L update — skipped within 30 min of a trade post, and dropped first when
                 the cap is near
    15:35        day closed: P&L stats (+ month so far)
Pyramid adds and partial bookings get no post of their own; they count in the trade's result.

The cap always keeps room for (a) the day-close post and (b) the exit of every position it
announced, so followers never see an entry without its exit or a day without its close. An
entry that would not leave that room is not announced (and its exit is then not posted either).

Safe by default:
  * DRY RUN unless X_LIVE=1. Dry run writes the exact text to logs/x_poster_dryrun.log and
    counts toward the cap exactly like a real post.
  * First start (no cursor) begins at the END of the outbox — it never replays old trades.
  * Events from an earlier day are never posted (only tallied).
  * Hourly lines older than SNAPSHOT_MAX_AGE are skipped; late trade posts say "(delayed)".
  * No links in posts (X charges far more for a post with a URL).
  * MODEL SIZE, not the owner's fills: every trade is shown at a fixed size by signal grade
    (STRONG 3, MODERATE 2, STRANGLE 1 per leg), so margin step-downs, the 14:00 half-size rule
    and profit-lock cuts never change what followers see. Its ₹ come from publish/follow.py — the
    same whole-lot ledger the Telegram channel uses — i.e. what a follower really makes following
    the posts in whole lots. Every ₹ figure is rounded to whole rupees before it is added up, so a
    day = the sum of its trade posts and a month = the sum of its days.

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
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

import paths as _paths
from publish import follow as F

logger = logging.getLogger("x_poster")

IST = ZoneInfo("Asia/Kolkata")
POST_URL = "https://api.x.com/2/tweets"
OUTBOX: Path = _paths.SIGNAL_OUTBOX
CURSOR: Path = _paths.LOGS_DIR / "x_poster_cursor.json"
DRYRUN_LOG: Path = _paths.LOGS_DIR / "x_poster_dryrun.log"
HOLIDAYS_FILE: Path = Path(__file__).with_name("nse_holidays.txt")
ENV_FILE = Path(os.environ.get("X_ENV_FILE", str(_paths.ROOT / "x.env")))
HANDLE = os.environ.get("X_HANDLE", "@ThetaDeskIN")

DAILY_CAP = 10
MODEL_LOTS = F.STANDARD                                    # STRONG 3 · MODERATE 2 · STRANGLE 1/leg (Rahul, 2026-11-26)
POLL_SEC = 15
GREETING_FROM, GREETING_UNTIL = dtime(9, 0), dtime(9, 15)
QUOTE_FROM, QUOTE_UNTIL = dtime(9, 5), dtime(9, 15)       # daily trading-wisdom line (publish/quotes.py)
SCAN_FROM, SCAN_UNTIL = dtime(9, 15), dtime(9, 45)
QUIET_AFTER_TRADE = 30 * 60         # no hourly line within this long after a trade post
SNAPSHOT_MAX_AGE = 20 * 60          # skip an hourly P&L line older than this
LATE_AFTER = 10 * 60                # trade posts older than this get "(delayed)"
MAX_LEN = 280
HASHTAGS = "#Nifty #OptionSelling #AlgoTrading"

_REASONS = {
    "SL_HIT": "Stop-loss hit", "PROFIT_BOOK": "Profit booked", "SIGNAL_EXIT": "Signal reversed",
    "STRANGLE_LEG_EXIT": "Leg closed on signal", "EOD": "End-of-day exit", "EOD_FLATTEN": "End-of-day exit",
    "TARGET": "Target hit", "MANUAL_EXIT": "Closed manually", "DAY_FLOOR": "Daily loss limit",
    "PROFIT_LOCK": "Trailing profit lock",
}
_BIAS = {
    "CALL_SELL": "Bearish — favours selling calls",
    "PUT_SELL": "Bullish — favours selling puts",
    "STRANGLE": "Range-bound — favours selling both sides",
    "NO_SIGNAL": "No clear edge yet",
}


# ── small helpers ────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)


def _ts(ev: dict) -> datetime:
    return datetime.fromisoformat(ev["ts"]).astimezone(IST)


def _rs(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.0f}"


def _pts(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.1f} pts"


def _px(x) -> str:
    return "~market" if x is None else f"₹{x:,.2f}"


def _num(x) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _day_label(d: date) -> str:
    return d.strftime("%a %-d %b")


def _reason(tag: str) -> str:
    return _REASONS.get(tag, tag.replace("_", " ").capitalize())


def _weight(s: str) -> int:
    return sum(2 if ord(c) > 0x2000 else 1 for c in s)


def _fit(text: str) -> str:
    """X counts most emoji as 2; stay under the limit by dropping the hashtag line first."""
    if _weight(text) <= MAX_LEN:
        return text
    text = "\n".join(l for l in text.split("\n") if not l.startswith("#"))
    if _weight(text) <= MAX_LEN:
        return text                                   # dropping the hashtags was enough: no "…"
    while _weight(text) > MAX_LEN - 1:
        text = text[:-1]
    return text.rstrip() + "…"


def load_holidays() -> set:
    try:
        return {l.split("#")[0].strip() for l in HOLIDAYS_FILE.read_text().splitlines() if l.split("#")[0].strip()}
    except FileNotFoundError:
        return set()


def is_market_day(d: date, holidays: Optional[set] = None) -> bool:
    holidays = load_holidays() if holidays is None else holidays
    return d.weekday() < 5 and d.isoformat() not in holidays


# ── post texts ───────────────────────────────────────────────────────────────

def quote_text(idx: int, d: date) -> str:
    from publish.quotes import QUOTES, tags_for
    q = QUOTES[idx % len(QUOTES)]
    return _fit(f"{q}\n\n— Theta Desk · {_day_label(d)}\n{tags_for(idx)}")      # the date keeps repeats unique


def greeting_text(d: date) -> str:
    return _fit(
        f"☀️ Good morning — {_day_label(d)}\n"
        f"Nifty option-selling algo, live from 10:00. Follow {HANDLE} for every entry & exit.\n"
        f"Size: 3 lots strong · 2 moderate · 1/leg strangle\n"
        f"Real-time + 1-lot alerts: ThetaDeskIN on Telegram\n"   # plain text: no link fee, no @-tag
        f"First scan 9:15\n{HASHTAGS}")


def scan_text(ev: dict) -> str:
    t = _ts(ev)
    bias = _BIAS.get(ev.get("action"), ev.get("action", "").replace("_", " ").title())
    score = f" ({ev['score']}/3)" if ev.get("action") in ("CALL_SELL", "PUT_SELL") and ev.get("score") is not None else ""
    lines = [f"📊 First scan {t:%H:%M} — {_day_label(t.date())}",
             f"Nifty spot {_num(ev.get('spot'))}",
             f"Bias: {bias}{score}"]
    if ev.get("resistance") or ev.get("support"):
        lines.append(f"Resistance {_num(ev.get('resistance'))} · Support {_num(ev.get('support'))}")
    if ev.get("pcr"):
        lines.append(f"PCR {ev['pcr']:.2f}")
    lines += ["Signals are re-checked at 10:00 — trading starts 10:00 sharp ⏰", HASHTAGS]
    return _fit("\n".join(lines))


def model_lots(ev: dict) -> int:
    return F.lots_for(F.STANDARD, ev.get("strength"), ev.get("structure"))


def _lots_word(n: int) -> str:
    return f"{n} lot{'s' if n != 1 else ''}"


def entry_text(ev: dict, late: bool) -> str:
    legs = ev["legs"]
    n = model_lots(ev)
    d = " (delayed)" if late else ""
    grade = {"STRONG": "strong signal", "MODERATE": "moderate signal"}.get(ev.get("strength") or "", "")
    if ev["structure"] == "STRANGLE":
        head = f"🔴 NEW TRADE{d} — Nifty strangle"
        what = f"{_lots_word(n)} each: " + " + ".join(f"{l['strike']} {l['leg']} @ {_px(l['price'])}" for l in legs)
        stop = f"spot above {_num(ev['sl_spot'])} / below {_num(ev.get('sl_spot_put'))}"
    else:
        l = legs[0]
        head = f"🔴 NEW TRADE{d} — Nifty" + (f", {grade}" if grade else "")
        what = f"{_lots_word(n)} {l['strike']} {l['leg']} @ {_px(l['price'])}"
        stop = f"spot {'above' if l['leg'] == 'CE' else 'below'} {_num(ev['sl_spot'])}"
    lines = [head, f"Sold {what}", f"Spot {_num(ev['spot'])} · Stop: {stop}"]
    if ev.get("hedge"):
        h = ev["hedge"]
        lines.append(f"Hedge: bought {_lots_word(n)} {h['strike']} {legs[0]['leg']} @ {_px(h['price'])}")
    lines += [f"{_ts(ev):%H:%M} IST", HASHTAGS]
    return _fit("\n".join(lines))


def exit_text(ev: dict, result_rs: Optional[int], lots: int, late: bool) -> str:
    if ev.get("structure") == "STRANGLE":
        what = "Nifty strangle" if ev["leg"] == "BOTH" else f"Nifty strangle, last leg {ev['strike']} {ev['leg']}"
    else:
        what = f"Nifty {ev['strike']} {ev['leg']}"
    icon = "✅" if (result_rs if result_rs is not None else ev.get("pnl") or 0) >= 0 else "❌"
    lines = [f"{icon} TRADE CLOSED{' (delayed)' if late else ''} — {what} @ {_px(ev.get('exit_price'))}",
             _reason(ev["reason"]) + (f" · {_pts(ev['points'])}" if ev.get("points") is not None else "")]
    if result_rs is not None:
        lines.append(f"Trade result: {_rs(result_rs)} ({_lots_word(lots)}{' each leg' if ev.get('structure') == 'STRANGLE' else ''})")
    lines.append(f"{_ts(ev):%H:%M} IST")
    return _fit("\n".join(lines))


def hourly_text(ev: dict, booked: int, open_rs: Optional[int]) -> str:
    """booked / open_rs are model-size rupees, already rounded."""
    lines = [f"⏱ {_ts(ev):%H:%M} IST — Nifty algo"]
    if ev.get("position_open") and open_rs is None:
        lines.append(f"Booked {_rs(booked)} · open P&L updating")
    else:
        lines.append(f"Day P&L: {_rs(booked + (open_rs or 0))}")
        if ev.get("position_open"):
            lines.append(f"Booked {_rs(booked)} · Open {_rs(open_rs)}")
    n = ev.get("trades_closed_today") or 0
    lines.append(f"{n} trade{'s' if n != 1 else ''} closed today" + (" · position open" if ev.get("position_open") else " · flat"))
    lines.append(HASHTAGS)
    return _fit("\n".join(lines))


def close_text(ev: dict, trades: list, day_total: int, month_total: int, month_days: int) -> str:
    d = _ts(ev).date()
    lines = [f"🏁 Day closed — {_day_label(d)}"]
    if not trades and not ev.get("trades_closed_today"):
        lines.append("No trade today: the signals never lined up, so the algo stayed out.")
    else:
        lines.append(f"Day P&L: {_rs(day_total)} (1 lot = 65 qty)")
        if trades:
            won = sum(1 for t in trades if t >= 0)
            lines.append(f"Trades: {len(trades)} · ✅ {won} · ❌ {len(trades) - won}")
            if len(trades) > 1:
                lines.append(f"Best {_rs(max(trades))} · Worst {_rs(min(trades))}")
    if ev.get("position_open"):
        lines.append("⚠️ A position is still open")
    lines.append(f"{d:%b} so far: {_rs(month_total)} ({month_days} day{'s' if month_days != 1 else ''})")
    lines.append(HASHTAGS)
    return _fit("\n".join(lines))


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


# ── state ────────────────────────────────────────────────────────────────────

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


def _fresh_day(d: date) -> dict:
    return {"date": d.isoformat(), "posts": 0, "greeted": False, "quoted": False, "scanned": False, "closed": False,
            "last_trade_post": None, "trades": []}


def _today(cur: dict, now: datetime) -> dict:
    day = cur.get("day")
    if not day or day.get("date") != now.date().isoformat():
        day = cur["day"] = _fresh_day(now.date())
    return day


def _reserve(cur: dict) -> int:
    """Posts that must stay available: the day close, plus the exit of every announced open position."""
    day = cur["day"]
    open_announced = sum(1 for p in cur.get("book", {}).values() if p.get("announced"))
    return (0 if day["closed"] else 1) + open_announced


def _room(cur: dict, extra_reserved: int = 0) -> bool:
    """Is there room for one more optional post while keeping the reserve (+extra) free?"""
    return cur["day"]["posts"] + 1 + _reserve(cur) + extra_reserved <= DAILY_CAP


class _Retry(Exception):
    pass


def _send(text: str, cur: dict, creds: dict, live: bool, poster, tag: str) -> bool:
    """Post (or dry-run) one text. True = done and counted. False = permanently skipped.
    Raises _Retry for a temporary failure (nothing is counted or advanced)."""
    if cur["day"]["posts"] >= DAILY_CAP:
        logger.warning(f"{tag}: daily cap {DAILY_CAP} reached — not posted")
        return False
    if live:
        ok, retryable, detail = poster(text, creds)
        if not ok and retryable:
            logger.warning(f"{tag}: will retry — {detail}")
            raise _Retry()
        if not ok:
            logger.error(f"{tag}: X refused, skipping — {detail}\n{text}")
            return False
        logger.info(f"{tag}: posted {detail}")
    else:
        DRYRUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DRYRUN_LOG, "a") as f:
            f.write(f"--- {tag} {now_ist().isoformat(timespec='seconds')}\n{text}\n\n")
        logger.info(f"{tag}: DRY RUN (see {DRYRUN_LOG.name})")
    cur["day"]["posts"] += 1
    return True


def _model_day(ev: dict, cur: dict) -> tuple[int, Optional[int]]:
    """Today's P&L at MODEL size, in whole ₹: (booked, open). booked = closed trades today + the
    already-booked part of the open position; open = its unrealised (None if no fresh mark)."""
    booked = sum(cur["day"]["trades"])
    if not ev.get("position_open"):
        return booked, 0
    pos = cur.get("book", {}).get(ev.get("position_id") or "")
    if not pos or "sizes" not in pos:
        return booked, None
    booked += round(F.realised(pos, "standard"))
    ov = F.open_value(pos, "standard", ev.get("unrealised"))
    return booked, (round(ov) if ov is not None else None)


# ── the decision for one event ───────────────────────────────────────────────

def _handle(ev: dict, cur: dict, now: datetime, holidays: set, creds, live, poster) -> None:
    book = cur.setdefault("book", {})
    day = cur["day"]
    t = ev["type"]
    pid = ev.get("position_id")
    ts = _ts(ev)
    today_event = ts.date() == now.date() and is_market_day(now.date(), holidays)
    age = (now - ts).total_seconds()
    tag = f"seq {ev['seq']} {t}"

    if t == "ENTRY":
        book[pid or "?"] = {**F.new_position(ev), "announced": False}
        # room for this entry AND its later exit, on top of the reserve
        if today_event and _room(cur, extra_reserved=1):
            if _send(entry_text(ev, age > LATE_AFTER), cur, creds, live, poster, tag):
                book[pid or "?"]["announced"] = True
                day["last_trade_post"] = ts.isoformat()
        elif today_event:
            logger.warning(f"{tag}: not announced — daily cap would not leave room for its exit and the day close")
        return

    if t == "ADD":
        pos = book.get(pid) if pid else None
        if pos and "sizes" in pos:
            F.step(pos, ev)                                  # no post; the ledger follows the add
        return

    if t == "EXIT":
        pos = book.get(pid) if pid else None
        if pos and "sizes" in pos:
            F.step(pos, ev)                                  # whole-lot ledger, same as Telegram
        if not ev.get("position_closed"):
            return                                           # partial booking / one strangle leg: no post
        if pos and "sizes" in pos:
            result_rs = round(F.realised(pos, "standard"))
            lots = F.size_state(pos, "standard")["n"]
        else:                                                # a position this poster never saw open
            lots = MODEL_LOTS["MODERATE"]
            result_rs = round(ev["points"] * F.LOT_SIZE * lots) if ev.get("points") is not None else None
        if today_event and result_rs is not None:
            day["trades"].append(result_rs)
        announced = bool(pos and pos.get("announced"))
        book.pop(pid, None)
        if today_event and announced:
            # this exit was reserved for: post it even when the cap is tight (only the hard cap stops it)
            _send(exit_text(ev, result_rs, lots, age > LATE_AFTER), cur, creds, live, poster, tag)
            day["last_trade_post"] = ts.isoformat()
        return

    if t == "SCAN":
        if (today_event and not day["scanned"] and SCAN_FROM <= ts.time() < SCAN_UNTIL
                and now.time() < SCAN_UNTIL and _room(cur)):
            _send(scan_text(ev), cur, creds, live, poster, tag)
            day["scanned"] = True
        return

    if t == "PNL_SNAPSHOT":
        if not today_event:
            return
        if ev.get("label") == "CLOSE":
            if day["closed"]:
                return
            day["closed"] = True                              # frees its own reserved slot for _send
            booked, open_rs = _model_day(ev, cur)
            day_total = booked + (open_rs or 0)
            hist = cur.setdefault("history", {})
            hist[ts.date().isoformat()] = day_total           # whole ₹, so the month = the sum of posted days
            month = ts.strftime("%Y-%m")
            month_days = [v for k, v in hist.items() if k.startswith(month)]
            try:
                _send(close_text(ev, day["trades"], day_total, sum(month_days), len(month_days)),
                      cur, creds, live, poster, tag)
            except _Retry:
                day["closed"] = False                         # retry later, history entry is idempotent
                raise
            return
        # hourly
        if age > SNAPSHOT_MAX_AGE:
            return
        if not ev.get("position_open") and not ev.get("trades_closed_today"):
            return                                            # nothing traded yet: the 9:15 post said 10:00
        last = day.get("last_trade_post")
        if last and (ts - datetime.fromisoformat(last)).total_seconds() < QUIET_AFTER_TRADE:
            return
        if _room(cur):
            booked, open_rs = _model_day(ev, cur)
            _send(hourly_text(ev, booked, open_rs), cur, creds, live, poster, tag)
        else:
            logger.info(f"{tag}: hourly skipped to keep room under the daily cap")
        return
    # anything unknown: no post


def run_once(creds: dict, live: bool, poster=post_to_x, now: Optional[datetime] = None) -> int:
    """One pass: the 9:00 greeting if due, then every new outbox event. Returns posts made."""
    now = (now or now_ist()).astimezone(IST)
    holidays = load_holidays()
    events = _read_events()
    cur = _read_cursor()
    if cur is None:                                          # first start: never replay history
        cur = {"seq": max((e.get("seq", 0) for e in events), default=0), "book": {}, "history": {}}
        _today(cur, now)
        _write_cursor(cur)
        logger.info(f"first start: cursor set to seq {cur['seq']} (history not replayed)")
    day = _today(cur, now)
    before = day["posts"]
    try:
        if (not day["greeted"] and is_market_day(now.date(), holidays)
                and GREETING_FROM <= now.time() < GREETING_UNTIL):
            _send(greeting_text(now.date()), cur, creds, live, poster, "greeting")
            day["greeted"] = True
            _write_cursor(cur)
        if (not day.get("quoted") and is_market_day(now.date(), holidays)
                and QUOTE_FROM <= now.time() < QUOTE_UNTIL):
            idx = int(cur.get("quote_idx", 0))
            if _send(quote_text(idx, now.date()), cur, creds, live, poster, f"quote {idx}"):
                cur["quote_idx"] = idx + 1                    # rotation only moves on when it really went out
            day["quoted"] = True
            _write_cursor(cur)
        for ev in sorted(events, key=lambda e: e.get("seq", 0)):
            if ev.get("seq", 0) <= cur["seq"]:
                continue
            snapshot = json.dumps(cur)                       # undo point if this event must be retried
            try:
                _handle(ev, cur, now, holidays, creds, live, poster)
            except _Retry:
                restored = json.loads(snapshot)
                cur.clear(); cur.update(restored)
                break
            cur["seq"] = ev["seq"]
            _write_cursor(cur)
    except _Retry:
        pass
    _write_cursor(cur)
    return cur["day"]["posts"] - before


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    _paths.ensure_dirs()
    creds, live = load_env()
    logger.info(f"X poster starting — {'LIVE' if live else 'DRY RUN'}; cap {DAILY_CAP}/day; outbox {OUTBOX}")
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
