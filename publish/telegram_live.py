"""
Telegram service: the public channel, personal DMs at each follower's size, and the Q&A bot.

Runs as its OWN process (systemd: telegram-live.service), never inside the trading bot:
    venv/bin/python -m publish.telegram_live

Channel (the 3/2/1 "standard" plan, same rupees as X), real time:
    first scan (09:15) · every entry, add, partial booking, exit · hourly P&L · day close
Personal DMs (only to people who pressed Start in the bot, with alerts on), at their plan:
    standard 3/2/1 or small 1 lot — entries, adds, bookings, exits as whole-lot instructions,
    and a day-close line with their own P&L.
Bot buttons: Trade & SL · Margin · Charges · P&L · Lot size · Alerts.

Settings in TG_ENV_FILE (default /opt/trading-algo/tg.env, chmod 600) — never the trading bot's .env:
    TG_BOT_TOKEN   a NEW bot from @BotFather (never the owner's private alerts bot: two processes
                   polling one token cause 409 Conflicts and break the owner's commands)
    TG_CHANNEL     @YourChannel (the bot must be an admin with "Post messages")
    TG_LIVE        1 = send to the channel and DMs; anything else = dry run to logs/tg_dryrun.log
                   (the bot still answers people who message it either way)

The service reads the Zerodha API key from the trading bot's .env and today's owner token,
ONLY to ask Zerodha for margin figures (a read-only call). It never places orders.
"""
from __future__ import annotations

import html
import json
import re
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

import paths as _paths
from publish import access as A
from publish import follow as F
from publish import x_poster as X           # shared formatting helpers, holiday calendar, IST clock

logger = logging.getLogger("telegram_live")

ENV_FILE = Path(os.environ.get("TG_ENV_FILE", str(_paths.ROOT / "tg.env")))
OUTBOX: Path = _paths.SIGNAL_OUTBOX
STATE: Path = _paths.LOGS_DIR / "tg_state.json"
SUBS: Path = _paths.LOGS_DIR / "tg_subscribers.json"
DRYRUN_LOG: Path = _paths.LOGS_DIR / "tg_dryrun.log"
OWNER_ENV: Path = _paths.ROOT / ".env"
LOT_SIZE = 65
POLL_SEC = 10
SNAPSHOT_MAX_AGE = 20 * 60
PLAN_LABEL = {"standard": "Standard · 3/2/1 lots", "small": "Small · 1 lot"}

# Approximate Zerodha F&O option charges (rates as of 2025). Shown as an estimate, never as exact.
CHARGES = {
    "brokerage_per_order": 20.0,        # flat per executed order
    "stt_sell": 0.001,                  # 0.1% of premium, sell side
    "exchange": 0.0003503,              # NSE options, on premium turnover
    "sebi_per_crore": 10.0,
    "stamp_buy": 0.00003,               # 0.003%, buy side
    "gst": 0.18,                        # on brokerage + exchange + SEBI
}

BUTTONS = [["📍 Trade & SL", "💰 Margin"], ["🧾 Charges", "📈 P&L"], ["⚙️ Lot size", "🔔 Alerts"]]
BUTTON_CMD = {"📍 Trade & SL": "/trade", "💰 Margin": "/margin", "🧾 Charges": "/charges",
              "📈 P&L": "/pnl", "⚙️ Lot size": "/size", "🔔 Alerts": "/alerts"}
COMMANDS = [
    {"command": "start", "description": "Start: pick your lot size and get trade alerts"},
    {"command": "trade", "description": "Open trade at your size, with stop-loss"},
    {"command": "margin", "description": "Margin needed for the trade at your size"},
    {"command": "charges", "description": "Estimated charges for the trade at your size"},
    {"command": "pnl", "description": "Today's and this month's P&L at your size"},
    {"command": "size", "description": "Change your lot size"},
    {"command": "alerts", "description": "Turn trade DMs on or off"},
    {"command": "help", "description": "What this bot does"},
]

_lock = threading.RLock()


# ── settings ─────────────────────────────────────────────────────────────────

def _read_env(path: Path) -> dict:
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def load_settings() -> dict:
    v = _read_env(ENV_FILE)
    get = lambda k: v.get(k) or os.environ.get(k, "")
    channel = get("TG_CHANNEL")
    if channel and not channel.startswith("@") and not channel.lstrip("-").isdigit():
        channel = "@" + channel
    try:
        delay = max(0, int(get("TG_CHANNEL_DELAY_MIN") or 15))
    except ValueError:
        delay = 15
    return {"token": get("TG_BOT_TOKEN"), "channel": channel, "live": get("TG_LIVE") == "1",
            "owner_id": get("TG_OWNER_ID"), "pay_contact": get("TG_PAY_CONTACT"), "delay_min": delay}


# ── Telegram API ─────────────────────────────────────────────────────────────

class TG:
    """Thin Telegram Bot API client. Tests replace `call`."""

    def __init__(self, token: str):
        self.token = token
        self.username = ""

    def call(self, method: str, **params) -> dict:
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                          timeout=params.get("timeout", 0) + 15)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "error_code": r.status_code, "description": r.text[:200]}

    def send(self, chat_id, text: str, **extra) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=safe_html(text)[:4000], parse_mode="HTML",
                         disable_web_page_preview=True, **extra)


def safe_html(text: str) -> str:
    """Telegram's HTML mode rejects a bare '&' (e.g. 'P&L'); escape any '&' that isn't already an entity."""
    return re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+);)", "&amp;", text)


def bot_button(username: str) -> Optional[dict]:
    """The button under every channel post: opens the bot (deep link, so we can tell where people came from)."""
    if not username:
        return None
    return {"inline_keyboard": [[{"text": "💬 Ask the bot · 1-lot alerts · margin",
                                  "url": f"https://t.me/{username}?start=channel"}]]}


def _retryable(resp: dict) -> bool:
    code = resp.get("error_code")
    return code is None or code == 429 or (isinstance(code, int) and code >= 500)


# ── state ────────────────────────────────────────────────────────────────────

def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except ValueError:
        logger.warning(f"{path.name} unreadable — starting from default")
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _fresh_day(d: date) -> dict:
    return {"date": d.isoformat(), "trades": [], "scanned": False, "closed": False, "last_snapshot": None}


def _day(st: dict, now: datetime) -> dict:
    if not st.get("day") or st["day"].get("date") != now.date().isoformat():
        st["day"] = _fresh_day(now.date())
    return st["day"]


def load_subs() -> dict:
    with _lock:
        return _load(SUBS, {})


def save_subs(subs: dict) -> None:
    with _lock:
        _save(SUBS, subs)


# ── message text ─────────────────────────────────────────────────────────────

def _lots(n) -> str:
    return f"{n} lot{'s' if n != 1 else ''}"


def _plan_lots(plan_name: str, pos: dict) -> int:
    return F.lots_for(F.PLANS[plan_name], pos.get("grade"), pos.get("structure"))


def _stop_line(pos: dict) -> str:
    if pos.get("structure") == "STRANGLE":
        return f"🛑 Stop: exit if Nifty spot goes above {X._num(pos.get('sl_spot'))} or below {X._num(pos.get('sl_spot_put'))}"
    leg = next(iter(pos["legs"]))
    side = "above" if leg == "CE" else "below"
    return f"🛑 Stop: exit if Nifty spot goes {side} {X._num(pos.get('sl_spot'))}"


def _hedge_note(pos: dict, buy: bool) -> str:
    if not pos.get("hedge") or pos.get("structure") == "STRANGLE":
        return ""
    return f" — and {'buy' if buy else 'sell'} the same lots of the {pos['hedge']['strike']} hedge"


def _actions(pos: dict, deltas: dict, prices: dict) -> list:
    lines = []
    for leg, d in deltas.items():
        strike = pos["legs"][leg]["strike"]
        px = prices.get(leg)
        at = f" @ ~{X._px(px)}" if px is not None else " at market"
        if d > 0:
            lines.append(f"Sell <b>+{_lots(d)}</b> {strike} {leg}{at}{_hedge_note(pos, buy=True)}")
        else:
            lines.append(f"Buy back <b>{_lots(-d)}</b> {strike} {leg}{at}{_hedge_note(pos, buy=False)}")
    return lines


def entry_msg(ev: dict, pos: dict, plan_name: str) -> str:
    n = _plan_lots(plan_name, pos)
    grade = {"STRONG": ", strong signal", "MODERATE": ", moderate signal"}.get(pos.get("grade") or "", "")
    name = "Nifty strangle" if pos["structure"] == "STRANGLE" else "Nifty"
    lines = [f"🔴 <b>NEW TRADE</b> — {name}{grade}"]
    for l in ev["legs"]:
        lines.append(f"Sell <b>{_lots(n)}</b> {l['strike']} {l['leg']} @ {X._px(l['price'])}")
    if ev.get("hedge") and pos["structure"] != "STRANGLE":
        h = ev["hedge"]
        lines.append(f"Hedge: buy <b>{_lots(n)}</b> {h['strike']} {ev['legs'][0]['leg']} @ {X._px(h['price'])}")
    lines.append(f"Expiry {ev.get('expiry')} · Spot {X._num(ev.get('spot'))}")
    lines.append(_stop_line(pos))
    lines.append(f"⏰ {X._ts(ev):%H:%M} IST")
    return "\n".join(lines)


def add_msg(ev: dict, pos: dict, plan_name: str, deltas: dict) -> str:
    st = pos["sizes"][plan_name]
    legs = ", ".join(f"{l['strike']} {l['leg']}" for l in ev["legs"])
    lines = [f"➕ <b>ADD</b> — Nifty {legs}"]
    acts = _actions(pos, deltas, {l["leg"]: l.get("price") for l in ev["legs"]})
    lines += acts or [f"No action at your size — keep your {_lots(max(st['open'].values()))}."]
    lines.append(f"Now <b>{_lots(max(st['open'].values()))}</b>" + (f" · avg ~{X._px(ev['blended_entry'])}" if ev.get("blended_entry") else ""))
    lines.append(f"⏰ {X._ts(ev):%H:%M} IST")
    return "\n".join(lines)


def exit_msg(ev: dict, pos: dict, plan_name: str, deltas: dict, slice_rs: Optional[int],
             result_rs: Optional[int]) -> str:
    st = pos["sizes"][plan_name]
    n = st["n"]
    approx = "" if st.get("exact", True) else "≈"
    legs = F.legs_of(ev)
    prices = {leg: (ev.get("exit_price") if len(legs) == 1 else None) for leg in legs}
    where = ("Nifty strangle" if len(legs) > 1 else f"Nifty {pos['legs'][legs[0]]['strike']} {legs[0]}") if legs else "Nifty"
    pts = f" · {X._pts(ev['points'])}" if ev.get("points") is not None else ""
    if ev.get("position_closed"):
        good = (result_rs if result_rs is not None else ev.get("pnl") or 0) >= 0
        lines = [f"{'✅' if good else '❌'} <b>TRADE CLOSED</b> — {where}"]
        lines += _actions(pos, deltas, prices) or ["Nothing left to close at your size."]
        lines.append(f"{X._reason(ev['reason'])}{pts}")
        if result_rs is not None:
            lines.append(f"Trade result: <b>{approx}{X._rs(result_rs)}</b> ({_lots(n)}"
                         + (" each leg" if pos['structure'] == 'STRANGLE' else "") + ")")
    else:
        lines = [f"💰 <b>{X._reason(ev['reason']).upper()}</b> — {where}"]
        acts = _actions(pos, deltas, prices)
        lines += acts or [f"No action at your size — keep your {_lots(max(st['open'].values()))}."]
        if acts and slice_rs is not None:
            lines.append(f"Booked: {approx}{X._rs(slice_rs)}{pts}")
        lines.append(f"<b>{_lots(max(st['open'].values()))}</b> still open")
    lines.append(f"⏰ {X._ts(ev):%H:%M} IST")
    return "\n".join(lines)


def scan_msg(ev: dict) -> str:
    text = X.scan_text(ev)
    return "\n".join(l for l in text.split("\n") if not l.startswith("#"))


def _plan_day(st: dict, plan_name: str) -> int:
    return sum(t["rs"][plan_name] for t in st["day"]["trades"])


def _open_rs(st: dict, ev: dict, plan_name: str) -> tuple[int, Optional[int]]:
    """(booked part of the open position, its unrealised) at a plan's size, whole ₹."""
    pos = st.get("positions", {}).get(ev.get("position_id") or "")
    if not pos:
        return 0, None
    ov = F.open_value(pos, plan_name, ev.get("unrealised"))
    return round(F.realised(pos, plan_name)), (round(ov) if ov is not None else None)


def hourly_msg(ev: dict, st: dict) -> str:
    booked = _plan_day(st, "standard")
    lines = [f"⏱ <b>{X._ts(ev):%H:%M} IST</b> — Nifty algo (3/2/1 plan)"]
    if ev.get("position_open"):
        part, open_rs = _open_rs(st, ev, "standard")
        booked += part
        if open_rs is None:
            lines.append(f"Booked {X._rs(booked)} · open P&L updating")
        else:
            lines.append(f"Day P&L: <b>{X._rs(booked + open_rs)}</b>")
            lines.append(f"Booked {X._rs(booked)} · Open {X._rs(open_rs)}")
        lines.append("Position open")
    else:
        lines.append(f"Day P&L: <b>{X._rs(booked)}</b> · flat")
    n = len(st["day"]["trades"])
    lines.append(f"{n} trade{'s' if n != 1 else ''} closed today")
    return "\n".join(lines)


def close_msg(ev: dict, st: dict, plan_name: str = "standard", personal: bool = False) -> str:
    d = X._ts(ev).date()
    trades = [t["rs"][plan_name] for t in st["day"]["trades"]]
    day_total = sum(trades)
    hist = st.get("history", {})
    month = d.strftime("%Y-%m")
    month_total = sum(t["rs"][plan_name] for k, ts in hist.items() if k.startswith(month) for t in ts)
    month_days = sum(1 for k in hist if k.startswith(month))
    head = f"🏁 <b>{'Your day' if personal else 'Day closed'}</b> — {X._day_label(d)}" + (f" ({PLAN_LABEL[plan_name]})" if personal else "")
    lines = [head]
    if not trades:
        lines.append("No trade today: the signals never lined up, so the algo stayed out.")
    else:
        won = sum(1 for t in trades if t >= 0)
        lines.append(f"Day P&L: <b>{X._rs(day_total)}</b> before charges")
        lines.append(f"Trades: {len(trades)} · ✅ {won} · ❌ {len(trades) - won}")
        if len(trades) > 1:
            lines.append(f"Best {X._rs(max(trades))} · Worst {X._rs(min(trades))}")
    lines.append(f"{d:%b} so far: {X._rs(month_total)} ({month_days} day{'s' if month_days != 1 else ''})")
    if not personal:
        lines.append("Plan: 3 lots strong · 2 moderate · 1/leg strangle (1 lot = 65 qty)")
    return "\n".join(lines)


# ── publisher ────────────────────────────────────────────────────────────────

class _Retry(Exception):
    pass


class Publisher:
    def __init__(self, tg: Optional[TG], settings: dict):
        self.tg = tg
        self.settings = settings

    def _out(self, chat_id, text: str, tag: str, required: bool, markup: Optional[dict] = None) -> None:
        """Send one message. `required` (the channel) raises _Retry on a temporary failure so the
        whole event is retried before any DM goes out; DMs are best effort (never duplicated)."""
        if not self.settings.get("live") or self.tg is None:
            DRYRUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(DRYRUN_LOG, "a") as f:
                f.write(f"--- {tag} → {chat_id}\n{text}\n\n")
            return
        resp = self.tg.send(chat_id, text, **({"reply_markup": markup} if markup else {}))
        if resp.get("ok"):
            return
        logger.warning(f"{tag} → {chat_id}: {resp.get('error_code')} {resp.get('description')}")
        if required and _retryable(resp):
            raise _Retry()
        if not required and resp.get("error_code") == 403:     # user blocked the bot
            subs = load_subs()
            if str(chat_id) in subs:
                subs[str(chat_id)]["alerts"] = False
                subs[str(chat_id)]["blocked"] = True
                save_subs(subs)

    def _channel(self, text: str, tag: str, ev: Optional[dict] = None, trade: bool = False) -> None:
        """Queue a channel post; it goes out `delay_min` minutes after the event (the public channel
        runs late — live alerts are for people with access). Order is always kept."""
        if not self.settings.get("channel"):
            return
        delay = self.settings.get("delay_min", 15)
        if trade and delay:
            text += f"\n\n🕒 Posted {delay} min after the trade. Live alerts: tap below."
        base = X._ts(ev) if ev else X.now_ist()
        due = (base + timedelta(minutes=delay)).isoformat(timespec="seconds")
        self._queue.append({"due": due, "text": text, "tag": tag})

    def flush_channel(self, st: dict, now: datetime) -> None:
        q = st.setdefault("channel_queue", [])
        while q and datetime.fromisoformat(q[0]["due"]) <= now:
            item = q[0]
            try:
                self._out(self.settings["channel"], item["text"], item["tag"], required=True,
                          markup=bot_button(self.tg.username if self.tg else ""))
            except _Retry:
                return                                       # keep it; retried next loop, order kept
            q.pop(0)
            _save(STATE, st)

    def _dms(self, build, tag: str) -> None:
        now = X.now_ist()
        owner = str(self.settings.get("owner_id") or "")
        for chat_id, sub in load_subs().items():
            if not sub.get("alerts", True) or sub.get("blocked"):
                continue
            A.ensure_trial(sub, now)
            if not A.has_access(sub, now, chat_id == owner):
                continue                                     # free month over and not paid
            text = build(sub.get("plan", "standard"))
            if text:
                self._out(chat_id, text, tag, required=False)

    def notices(self, now: datetime) -> None:
        """Reminders 5 and 1 days before access ends, and a notice when it ends."""
        owner = str(self.settings.get("owner_id") or "")
        subs = load_subs()
        changed = False
        for chat_id, sub in subs.items():
            A.ensure_trial(sub, now)
            due = A.due_notice(sub, now, chat_id == owner)
            if due is None:
                continue
            changed = True
            self._out(chat_id, notice_text(sub, due, self.settings), f"notice {due}d", required=False,
                      markup=pay_button())
        if changed:
            save_subs(subs)

    def handle(self, ev: dict, st: dict, now: datetime, holidays: set) -> None:
        t = ev["type"]
        ts = X._ts(ev)
        today = ts.date() == now.date() and X.is_market_day(now.date(), holidays)
        day = st["day"]
        positions = st.setdefault("positions", {})
        pid = ev.get("position_id")
        tag = f"seq {ev['seq']} {t}"

        if t == "ENTRY":
            pos = F.new_position(ev)
            positions[pid or "?"] = pos
            st["last_entry"] = pos
            if today:
                self._channel(entry_msg(ev, pos, "standard"), tag, ev, trade=True)
                self._dms(lambda plan: entry_msg(ev, pos, plan), tag)
            return

        if t in ("ADD", "EXIT"):
            pos = positions.get(pid or "")
            if pos is None:
                return                                       # a position this service never saw open
            res = F.step(pos, ev)
            deltas = {plan: r["deltas"] for plan, r in res.items()}
            if t == "ADD":
                if today:
                    self._channel(add_msg(ev, pos, "standard", deltas["standard"]), tag, ev, trade=True)
                    self._dms(lambda plan: add_msg(ev, pos, plan, deltas[plan]), tag)
                return
            closed = bool(ev.get("position_closed"))
            rec = {"rs": {plan: round(F.realised(pos, plan)) for plan in F.PLANS},
                   "grade": pos.get("grade"), "structure": pos.get("structure")}
            if closed:
                if today:
                    day["trades"].append(rec)
                    st.setdefault("history", {}).setdefault(day["date"], []).append(rec)
                positions.pop(pid, None)

            def build(plan):
                return exit_msg(ev, pos, plan, deltas[plan], round(res[plan]["slice_rs"]),
                                rec["rs"][plan] if closed else None)
            if today:
                self._channel(build("standard"), tag, ev, trade=True)
                self._dms(build, tag)
            return

        if t == "SCAN":
            if today and not day["scanned"] and X.SCAN_FROM <= ts.time() < X.SCAN_UNTIL:
                self._channel(scan_msg(ev), tag, ev)
                day["scanned"] = True
            return

        if t == "PNL_SNAPSHOT":
            day["last_snapshot"] = ev
            if not today:
                return
            if ev.get("label") == "CLOSE":
                if day["closed"]:
                    return
                st.setdefault("history", {}).setdefault(day["date"], [])
                self._channel(close_msg(ev, st), tag, ev)
                if day["trades"]:
                    self._dms(lambda plan: close_msg(ev, st, plan, personal=True), tag)
                day["closed"] = True
                return
            if (now - ts).total_seconds() > SNAPSHOT_MAX_AGE:
                return
            if ev.get("position_open") or day["trades"]:
                self._channel(hourly_msg(ev, st), tag, ev)

    def run_once(self, now: Optional[datetime] = None) -> None:
        now = (now or X.now_ist()).astimezone(X.IST)
        holidays = X.load_holidays()
        events = _read_outbox()
        st = _load(STATE, None)
        if st is None:                                       # first start: never replay history
            st = {"seq": max((e.get("seq", 0) for e in events), default=0), "positions": {}, "history": {}}
            _day(st, now)
            _save(STATE, st)
            logger.info(f"first start: cursor at seq {st['seq']} (history not replayed)")
            return
        _day(st, now)
        self._queue = st.setdefault("channel_queue", [])
        for ev in sorted(events, key=lambda e: e.get("seq", 0)):
            if ev.get("seq", 0) <= st["seq"]:
                continue
            self.handle(ev, st, now, holidays)               # DMs now; channel posts are queued
            st["seq"] = ev["seq"]
            _save(STATE, st)
        self.flush_channel(st, now)
        _save(STATE, st)
        self.notices(now)


def _read_outbox() -> list:
    if not OUTBOX.exists():
        return []
    out = []
    for line in OUTBOX.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ── margin and charges ───────────────────────────────────────────────────────

def _kite():
    from kiteconnect import KiteConnect
    api_key = _read_env(OWNER_ENV).get("KITE_API_KEY", "")
    tok = json.loads(_paths.OWNER_TOKEN_FILE.read_text())
    if tok.get("date") != date.today().isoformat():
        raise RuntimeError("today's Zerodha login hasn't happened yet")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(tok["access_token"])
    return kite


_margin_cache: dict = {}


def margin_for(pos: dict, n: int, kite_factory=None) -> dict:
    """Zerodha's own margin for the trade at n lots (with its hedge), cached 5 minutes."""
    key = (pos.get("id"), n)
    hit = _margin_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    qty = n * LOT_SIZE
    orders = []
    if pos.get("hedge") and pos.get("structure") != "STRANGLE":
        orders.append({"exchange": "NFO", "tradingsymbol": pos["hedge"]["symbol"], "transaction_type": "BUY",
                       "variety": "regular", "product": "NRML", "order_type": "MARKET", "quantity": qty, "price": 0})
    for leg in pos["legs"].values():
        orders.append({"exchange": "NFO", "tradingsymbol": leg["symbol"], "transaction_type": "SELL",
                       "variety": "regular", "product": "NRML", "order_type": "MARKET", "quantity": qty, "price": 0})
    r = (kite_factory or _kite)().basket_order_margins(orders, consider_positions=False, mode="compact")
    out = {"total": float(r["final"]["total"]), "at": X.now_ist().strftime("%H:%M")}
    _margin_cache[key] = (time.time(), out)
    return out


def charges_for(pos: dict, n: int, exit_prices: Optional[dict] = None) -> dict:
    """Estimated round-trip charges at n lots. Exits default to the entry price (a flat trade)."""
    qty = n * LOT_SIZE
    c = CHARGES
    sell_turn = buy_turn = 0.0
    orders = 0
    for leg, l in pos["legs"].items():
        entry = l.get("price") or 0
        exit_ = (exit_prices or {}).get(leg, entry)
        sell_turn += entry * qty          # sold to open
        buy_turn += exit_ * qty           # bought back to close
        orders += 2
    h = pos.get("hedge") if pos.get("structure") != "STRANGLE" else None
    if h:
        hp = h.get("price") or 0
        buy_turn += hp * qty              # bought to open
        sell_turn += hp * qty             # sold to close (same price assumed)
        orders += 2
    turnover = sell_turn + buy_turn
    brokerage = orders * c["brokerage_per_order"]
    stt = sell_turn * c["stt_sell"]
    exch = turnover * c["exchange"]
    sebi = turnover * c["sebi_per_crore"] / 1e7
    stamp = buy_turn * c["stamp_buy"]
    gst = (brokerage + exch + sebi) * c["gst"]
    total = brokerage + stt + exch + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp,
            "gst": gst, "total": total, "orders": orders}


# ── the bot (people who message it) ──────────────────────────────────────────

_stats_cache: dict = {}


def risk_stats(journal: Optional[Path] = None) -> Optional[dict]:
    """Real per-lot loss figures from the owner's trade journal (cached 1 hour): what the welcome
    message shows next to the "about 1%" aim, so the claim is never stated without the record."""
    journal = journal or _paths.JOURNAL_FILE
    hit = _stats_cache.get(str(journal))
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    import csv
    try:
        rows = list(csv.DictReader(open(journal)))
    except (FileNotFoundError, OSError):
        return None
    per_lot = []
    for r in rows:
        try:
            lots = float(r["lots"]) / LOT_SIZE
            if lots > 0:
                per_lot.append(float(r["net_pnl"]) / lots)
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    losses = [p for p in per_lot if p < 0]
    out = None
    if len(per_lot) >= 20 and losses:
        out = {"trades": len(per_lot), "avg_loss": sum(losses) / len(losses), "worst": min(losses),
               "over_2000": sum(1 for p in losses if p < -2000)}
    _stats_cache[str(journal)] = (time.time(), out)
    return out


def welcome_text(first_name: str, channel: str) -> str:
    s = risk_stats()
    record = ""
    if s:
        record = (f"Our record over {s['trades']} trades: a losing trade cost ₹{abs(s['avg_loss']):,.0f} per lot on "
                  f"average; the worst cost ₹{abs(s['worst']):,.0f}.\n")
    return (f"👋 Welcome{', ' + html.escape(first_name) if first_name else ''}!\n\n"
            f"I send you every trade of our Nifty option-selling algo the moment it happens — entries, adds, "
            f"bookings and exits — sized for you, with your P&L.\n"
            + (f"The public feed is {channel}.\n" if channel else "")
            + "\n<b>Pick your size by your trading capital</b>\n"
              "• <b>₹2 lakh → Small · 1 lot</b> on every trade.\n"
              "  A 1-lot trade needs only about ₹1.05–1.1 lakh of margin, but we recommend joining only with "
              "₹2 lakh: preserving capital comes first.\n"
              "• <b>₹5 lakh → Standard · 3/2/1</b>: 3 lots on strong signals, 2 on moderate, 1 per leg on strangles.\n"
              "  It can be placed with about ₹3.2–3.3 lakh, but we recommend ₹5 lakh for the same reason.\n\n"
              "We aim to risk about 1% of capital on a trade (≈ ₹2,000 per lot).\n"
            + record
            + "\n👀 <b>Start by watching.</b> Follow the trades for a few weeks before you put money in, and take "
              "the paid plan only once you're confident.\n\n"
              "Use the buttons below any time for the open trade, margin, charges and P&L.\n\n"
              "<i>Not investment advice. Past results don't guarantee future ones. Trade only what you can "
              "afford to lose.</i>")


def _current(st: dict) -> tuple[Optional[dict], bool]:
    """(position, is_open): the open position, else the latest entry (for margin/charges)."""
    positions = st.get("positions") or {}
    if positions:
        return next(iter(positions.values())), True
    return st.get("last_entry"), False


def _keyboard() -> dict:
    return {"keyboard": [[{"text": b} for b in row] for row in BUTTONS], "resize_keyboard": True, "is_persistent": True}


def pay_button() -> dict:
    return {"inline_keyboard": [[{"text": "💳 I want to continue", "callback_data": "pay:request"}]]}


def _date(d: datetime) -> str:
    return d.astimezone(X.IST).strftime("%-d %b %Y")


def access_line(sub: dict, now: datetime, is_owner: bool) -> str:
    st = A.status(sub, now, is_owner)
    end = A.access_until(sub)
    if st == "owner":
        return "👑 Owner access"
    if st == "paid":
        return f"✅ Paid access until <b>{_date(end)}</b> ({A.days_left(sub, now)} days left)"
    if st == "trial":
        return f"🎁 Free access until <b>{_date(end)}</b> ({A.days_left(sub, now)} days left)"
    return f"⏳ Access ended on {_date(end)}" if end else "⏳ No access"


def notice_text(sub: dict, due: int, settings: dict) -> str:
    end = A.access_until(sub)
    what = "subscription" if sub.get("paid_until") else "free month"
    contact = settings.get("pay_contact")
    how = "To continue, tap the button below and we'll get back to you here" + (f", or contact {contact}" if contact else "") + "."
    if due:
        return (f"⏳ Your {what} ends on <b>{_date(end)}</b> — {due} day{'s' if due != 1 else ''} left.\n{how}")
    channel = settings.get("channel") or "the public channel"
    return (f"⏳ Your {what} ended on <b>{_date(end)}</b>. Live trade alerts are paused.\n"
            f"{channel} still shows every trade, {settings.get('delay_min', 15)} minutes late.\n{how}")


def expired_text(sub: dict, settings: dict) -> str:
    return notice_text(sub, 0, settings)


OWNER_HELP = ("👑 <b>Owner commands</b>\n"
              "/members — everyone, status and end date\n"
              "/paid &lt;who&gt; &lt;months&gt; [amount] — record a payment, add months\n"
              "/extend &lt;who&gt; &lt;days&gt; — add free days\n"
              "/revoke &lt;who&gt; — end access now\n"
              "/msg &lt;who&gt; &lt;text&gt; — message someone through the bot\n"
              "&lt;who&gt; = chat id, @username or the start of their name")


def owner_command(cmd: str, args: list, subs: dict, now: datetime, settings: dict, send) -> Optional[str]:
    """Owner-only commands. Returns the reply, or None if `cmd` isn't one of them."""
    if cmd == "/owner":
        return OWNER_HELP
    if cmd == "/members":
        if not subs:
            return "No one has started the bot yet."
        rows = []
        owner = str(settings.get("owner_id") or "")
        for cid, sub in sorted(subs.items(), key=lambda kv: A.access_until(kv[1]) or now):
            A.ensure_trial(sub, now)
            st = A.status(sub, now, cid == owner)
            end = A.access_until(sub)
            paid = sum((p.get("amount") or 0) for p in sub.get("payments", []))
            rows.append(f"<code>{cid}</code> {html.escape(sub.get('name') or '?')}"
                        + (f" @{sub['username']}" if sub.get("username") else "")
                        + f" · {sub.get('plan', 'standard')} · <b>{st}</b>"
                        + (f" → {_date(end)}" if end and st != "owner" else "")
                        + (f" · paid ₹{paid:,.0f}" if paid else "")
                        + (" · blocked bot" if sub.get("blocked") else ""))
        counts = {}
        for cid, sub in subs.items():
            k = A.status(sub, now, cid == owner)
            counts[k] = counts.get(k, 0) + 1
        return (f"👥 <b>{len(subs)} members</b> · " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                + "\n\n" + "\n".join(rows[:60]) + ("\n…" if len(rows) > 60 else ""))
    if cmd not in ("/paid", "/extend", "/revoke", "/msg"):
        return None
    if not args:
        return OWNER_HELP
    found = A.find(subs, args[0])
    if not found:
        return f"No member matches “{html.escape(args[0])}”. Try /members."
    if len(found) > 1:
        return "More than one match: " + ", ".join(f"<code>{c}</code> {html.escape(subs[c].get('name') or '')}" for c in found)
    cid = found[0]
    sub = subs[cid]
    A.ensure_trial(sub, now)
    name = html.escape(sub.get("name") or cid)
    if cmd == "/paid":
        try:
            months = int(args[1])
            amount = float(args[2]) if len(args) > 2 else None
            assert 1 <= months <= 24
        except (IndexError, ValueError, AssertionError):
            return "Usage: /paid &lt;who&gt; &lt;months 1–24&gt; [amount]"
        end = A.add_paid(sub, now, months, amount, "owner")
        save_subs(subs)
        send(cid, f"✅ Payment received — thank you!\nLive trade alerts are on until <b>{_date(end)}</b>.", _keyboard())
        return f"✅ {name}: paid {months} month{'s' if months != 1 else ''}" + (f" (₹{amount:,.0f})" if amount else "") + f" → access until {_date(end)}. They've been told."
    if cmd == "/extend":
        try:
            days = int(args[1])
            assert 1 <= days <= 365
        except (IndexError, ValueError, AssertionError):
            return "Usage: /extend &lt;who&gt; &lt;days 1–365&gt;"
        end = A.extend(sub, now, days)
        save_subs(subs)
        send(cid, f"🎁 Your access has been extended until <b>{_date(end)}</b>.", _keyboard())
        return f"✅ {name}: +{days} days → access until {_date(end)}. They've been told."
    if cmd == "/revoke":
        A.revoke(sub, now)
        save_subs(subs)
        return f"⛔ {name}: access ended now. (They were not messaged.)"
    if cmd == "/msg":
        text = " ".join(args[1:]).strip()
        if not text:
            return "Usage: /msg &lt;who&gt; &lt;text&gt;"
        send(cid, f"💬 <b>From Theta Desk</b>\n{html.escape(text)}", None)
        return f"✉️ Sent to {name}."
    return None


def _size_buttons() -> dict:
    return {"inline_keyboard": [[{"text": "Small · 1 lot  (₹2 lakh capital)", "callback_data": "plan:small"}],
                                [{"text": "Standard · 3/2/1  (₹5 lakh capital)", "callback_data": "plan:standard"}]]}


def reply_for(text: str, chat_id: str, first_name: str, settings: dict, username: str = "",
              send=None) -> tuple[str, Optional[dict]]:
    """The bot's answer to one message: (html text, reply_markup or None). `send(chat_id, text, markup)`
    delivers side messages (owner commands that notify a member)."""
    send = send or (lambda *a: None)
    words = BUTTON_CMD.get(text.strip(), text.strip()).split() if text.strip() else []
    cmd = words[0].split("@")[0].lower() if words else ""
    now = X.now_ist()
    subs = load_subs()
    sub = subs.get(chat_id)
    st = _load(STATE, {}) or {}
    channel = settings.get("channel") or ""
    is_owner = bool(settings.get("owner_id")) and chat_id == str(settings["owner_id"])

    if sub is not None and username and sub.get("username") != username:
        sub["username"] = username
        save_subs(subs)

    if is_owner:
        out = owner_command(cmd, words[1:], subs, now, settings, send)
        if out is not None:
            return (out, None)

    if cmd == "/start" or sub is None:
        if sub is None:
            sub = subs[chat_id] = {"plan": "standard", "alerts": True, "name": first_name[:40],
                                   "username": username or None, "joined": now.isoformat(timespec="seconds")}
            A.ensure_trial(sub, now)
            save_subs(subs)
        A.ensure_trial(sub, now)
        if not A.has_access(sub, now, is_owner):
            return (welcome_text(first_name, channel) + "\n\n" + expired_text(sub, settings), pay_button())
        return (welcome_text(first_name, channel) + "\n\n" + access_line(sub, now, is_owner), _size_buttons())

    A.ensure_trial(sub, now)
    if not A.has_access(sub, now, is_owner):
        return (expired_text(sub, settings), pay_button())

    plan = sub.get("plan", "standard")

    if cmd in ("/size", "/lots"):
        return (f"Your size: <b>{PLAN_LABEL[plan]}</b>\nPick a new one:", _size_buttons())

    if cmd == "/alerts":
        sub["alerts"] = not sub.get("alerts", True)
        sub.pop("blocked", None)
        save_subs(subs)
        return (("🔔 Trade alerts are <b>on</b>." if sub["alerts"] else "🔕 Trade alerts are <b>off</b>. Tap again to turn them on."),
                _keyboard())

    if cmd in ("/trade", "/sl"):
        pos, is_open = _current(st)
        if not pos or not is_open:
            return ("No open trade right now. New trades can start between 10:00 and 14:55 IST on market days.", _keyboard())
        size = pos.get("sizes", {}).get(plan) or {"open": {leg: _plan_lots(plan, pos) for leg in pos["legs"]}}
        lines = [f"📍 <b>Open trade</b> ({PLAN_LABEL[plan]})"]
        for leg, l in pos["legs"].items():
            lines.append(f"Short <b>{_lots(size['open'].get(leg, 0))}</b> {l['strike']} {leg} · entry {X._px(l['price'])}")
        if pos.get("hedge") and pos.get("structure") != "STRANGLE":
            lines.append(f"Hedge: long {pos['hedge']['strike']} {next(iter(pos['legs']))}")
        lines.append(_stop_line(pos))
        snap = (st.get("day") or {}).get("last_snapshot") or {}
        ov = F.open_value(pos, plan, snap.get("unrealised")) if snap.get("position_open") else None
        if ov is not None:
            lines.append(f"Open P&L at {X._ts(snap):%H:%M}: {X._rs(round(ov))}")
        return ("\n".join(lines), _keyboard())

    if cmd == "/margin":
        pos, is_open = _current(st)
        if not pos:
            return ("No trade yet to price. Ask again once the algo opens one.", _keyboard())
        n = _plan_lots(plan, pos)
        try:
            m = margin_for(pos, n)
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"margin lookup failed: {e}")
            return ("Couldn't get live margin from Zerodha right now — please try again in a few minutes.", _keyboard())
        legs = " + ".join(f"{l['strike']} {leg}" for leg, l in pos["legs"].items())
        return ((f"💰 <b>Margin</b> for {'the open' if is_open else 'the last'} trade at your size\n"
                 f"{_lots(n)}{' each leg' if pos.get('structure') == 'STRANGLE' else ''} · {legs}"
                 f"{' with hedge' if pos.get('hedge') and pos.get('structure') != 'STRANGLE' else ''}\n"
                 f"Needed: <b>₹{m['total']:,.0f}</b> (≈ ₹{m['total'] / n:,.0f} per lot)\n"
                 f"<i>Zerodha's figure at {m['at']} IST. Your broker may differ; keep a buffer.</i>"), _keyboard())

    if cmd == "/charges":
        pos, is_open = _current(st)
        if not pos:
            return ("No trade yet to price. Ask again once the algo opens one.", _keyboard())
        n = _plan_lots(plan, pos)
        c = charges_for(pos, n)
        return ((f"🧾 <b>Estimated charges</b>, full round trip at your size ({_lots(n)})\n"
                 f"Brokerage ({c['orders']} orders): ₹{c['brokerage']:,.0f}\n"
                 f"STT: ₹{c['stt']:,.0f} · Exchange: ₹{c['exchange']:,.0f}\n"
                 f"GST: ₹{c['gst']:,.0f} · Stamp + SEBI: ₹{c['stamp'] + c['sebi']:,.0f}\n"
                 f"<b>Total ≈ ₹{c['total']:,.0f}</b>\n"
                 f"<i>Estimate at Zerodha's rates, assuming exit near the entry price. Check your broker's calculator.</i>"),
                _keyboard())

    if cmd == "/pnl":
        if not st.get("day"):
            return ("No trades recorded yet.", _keyboard())
        snap = (st.get("day") or {}).get("last_snapshot") or {}
        day_rs = _plan_day(st, plan)
        extra = ""
        if snap.get("position_open"):
            part, open_rs = _open_rs(st, snap, plan)
            day_rs += part
            if open_rs is not None:
                day_rs += open_rs
                extra = f" (incl. open {X._rs(open_rs)})"
        month = X.now_ist().strftime("%Y-%m")
        month_rs = sum(t["rs"][plan] for k, ts in (st.get("history") or {}).items()
                       if k.startswith(month) and k != st["day"]["date"] for t in ts) + day_rs
        return ((f"📈 <b>P&L at your size</b> ({PLAN_LABEL[plan]})\n"
                 f"Today: <b>{X._rs(day_rs)}</b>{extra}\n"
                 f"{X.now_ist():%b} so far: <b>{X._rs(month_rs)}</b>\n<i>Before charges.</i>"), _keyboard())

    return ((f"I post every trade of our Nifty option-selling algo, sized for you.\n"
             f"Your size: <b>{PLAN_LABEL[plan]}</b> · alerts {'on' if sub.get('alerts', True) else 'off'}\n"
             f"{access_line(sub, now, is_owner)}\n\n"
             f"Buttons: 📍 open trade & stop · 💰 margin · 🧾 charges · 📈 P&L · ⚙️ size · 🔔 alerts"), _keyboard())


def on_callback(data: str, chat_id: str, settings: Optional[dict] = None, send=None) -> str:
    settings = settings or {}
    send = send or (lambda *a: None)
    if data == "pay:request":
        subs = load_subs()
        sub = subs.get(chat_id) or {}
        owner = settings.get("owner_id")
        now = X.now_ist()
        if owner:
            A.ensure_trial(sub, now) if sub else None
            send(owner, (f"💳 <b>Payment request</b>\n{html.escape(sub.get('name') or '?')}"
                         + (f" @{sub['username']}" if sub.get("username") else "")
                         + f" · <code>{chat_id}</code> · {PLAN_LABEL.get(sub.get('plan', 'standard'))}\n"
                         + (access_line(sub, now, False) if sub else "")
                         + f"\nReply: /msg {chat_id} &lt;text&gt; · after payment: /paid {chat_id} 1 &lt;amount&gt;"), None)
            sub["pay_requested"] = now.isoformat(timespec="seconds")
            if chat_id in subs:
                save_subs(subs)
        contact = settings.get("pay_contact")
        return ("🙏 Thanks! We've got your request and will message you here shortly with payment details."
                + (f"\nOr contact {contact}." if contact else ""))
    if data.startswith("plan:") and data[5:] in F.PLANS:
        subs = load_subs()
        sub = subs.setdefault(chat_id, {"alerts": True, "joined": X.now_ist().isoformat(timespec="seconds")})
        sub["plan"] = data[5:]
        save_subs(subs)
        return f"✅ Size set: <b>{PLAN_LABEL[sub['plan']]}</b>. You'll get every trade sized like this."
    return "Sorry, I didn't get that."


def bot_loop(tg: TG, settings_ref: dict, stop: threading.Event) -> None:
    def side(chat_id, text, markup=None):
        r = tg.send(str(chat_id), text, **({"reply_markup": markup} if markup else {}))
        if not r.get("ok"):
            logger.warning(f"side message → {chat_id}: {r.get('error_code')} {r.get('description')}")
    offset = None
    while not stop.is_set():
        try:
            resp = tg.call("getUpdates", timeout=25, allowed_updates=["message", "callback_query"],
                           **({"offset": offset} if offset else {}))
            if not resp.get("ok"):
                logger.warning(f"getUpdates: {resp.get('error_code')} {resp.get('description')}")
                time.sleep(10)
                continue
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        q = u["callback_query"]
                        chat_id = str(q["message"]["chat"]["id"])
                        tg.call("answerCallbackQuery", callback_query_id=q["id"])
                        tg.send(chat_id, on_callback(q.get("data", ""), chat_id, settings_ref, side),
                                reply_markup=_keyboard())
                    elif "message" in u and u["message"].get("chat", {}).get("type") == "private":
                        m = u["message"]
                        text, markup = reply_for(m.get("text", ""), str(m["chat"]["id"]),
                                                 m.get("from", {}).get("first_name", ""), settings_ref,
                                                 m.get("from", {}).get("username") or "", side)
                        tg.send(str(m["chat"]["id"]), text, **({"reply_markup": markup} if markup else {}))
                except Exception as e:                       # noqa: BLE001 — one bad message never stops the bot
                    logger.exception(f"update {u.get('update_id')}: {e}")
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"bot loop: {e}")
            time.sleep(10)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    _paths.ensure_dirs()
    settings = load_settings()
    while not settings["token"]:
        logger.info(f"waiting for TG_BOT_TOKEN in {ENV_FILE} …")
        time.sleep(30)
        settings = load_settings()
    tg = TG(settings["token"])
    me = tg.call("getMe")
    tg.username = (me.get("result") or {}).get("username", "")
    tg.call("setMyCommands", commands=COMMANDS)
    logger.info(f"Telegram live: bot @{tg.username} · channel {settings['channel'] or '(none)'} · "
                f"{'LIVE' if settings['live'] else 'DRY RUN'}")
    stop = threading.Event()
    threading.Thread(target=bot_loop, args=(tg, settings, stop), daemon=True).start()
    pub = Publisher(tg, settings)
    while True:
        try:
            new = load_settings()
            if new["live"] != settings["live"] or new["channel"] != settings["channel"]:
                logger.info(f"settings changed: {'LIVE' if new['live'] else 'DRY RUN'} · channel {new['channel']}")
            settings.update(new)
            pub.run_once()
        except Exception as e:                               # noqa: BLE001
            logger.exception(f"publisher: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
