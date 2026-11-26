"""
Signal feed: a write-only record of what the bot does, for the public channels (X, Telegram).

The live bot calls the functions below at each entry, pyramid add, exit and on the hourly P&L
snapshot. Each call appends one JSON line to an outbox file (paths.SIGNAL_OUTBOX). A separate
publisher process reads the outbox and posts — so a slow or broken X / Telegram can never delay,
block or crash a real trade.

Rules this module keeps:
  * Never raises. Every public function swallows and logs its own errors.
  * Never talks to the network or the broker. It only writes two small local files.
  * Never reads back its own outbox. The bot's behaviour cannot depend on it.

Event schema (v1) — one JSON object per line:
  common:  v, id, seq, ts (ISO with offset), date, type, instrument, lot_size, position_id, structure
  ENTRY:   legs[{leg, strike, symbol, side, price, lots}], hedge?, spot, sl_spot, sl_spot_put?, lots_open, entry_lots,
           strength (signal grade: STRONG / MODERATE / STRANGLE; None if unknown)
  ADD:     legs[...] (the slice added, at its fill-request price), lots_added, lots_open, blended_entry
  EXIT:    leg ("CE" | "PE" | "BOTH"), strike, exit_price, lots_closed, lots_open (after), position_closed,
           reason, pnl (₹, owner's, net of hedge), points (₹ per share = per unit of premium),
           pnl_per_start_lot (pnl ÷ starting lots of the position)
  SCAN:    action, strength, score, spot, pcr, resistance, support, call_wall, put_wall (observe-only scans)
  PNL_SNAPSHOT: label, day_realised, unrealised (None if no fresh mark), day_total, mark_age_sec,
           day_per_start_lot, open_per_start_lot, trades_closed_today, position_open, lots_open, spot

"Per start lot" is the replica scale: a follower who copies every add and booking in proportion to
their own starting size makes the owner's P&L × (their start lots ÷ owner's start lots).
How fractions are rounded for a 1-lot follower is the publisher's decision, not this module's.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import paths as _paths
from config import INDEX, NIFTY_LOT_SIZE

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
INSTRUMENT = INDEX                # "NIFTY" for the live bot; BANKNIFTY / SENSEX on the paper instances
MARK_STALE_SEC = 20 * 60          # a P&L mark older than this is reported as unknown, not as a number

OUTBOX: Path = _paths.SIGNAL_OUTBOX
STATE: Path = _paths.SIGNAL_FEED_STATE
ENABLED = os.environ.get("SIGNAL_FEED", "1").strip() not in ("0", "false", "off", "")

_lock = threading.Lock()
_mark: Optional[dict] = None       # last unrealised P&L seen by the bot (memory only)

# action on the bot's TradeState  ->  legs it touches
_LEGS_OF = {
    "CALL_SELL": ("CE",), "PUT_SELL": ("PE",),
    "STRANGLE_CE": ("CE",), "STRANGLE_PE": ("PE",),
    "STRANGLE": ("CE", "PE"),
}


# ── tiny helpers ──────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now().astimezone()


def _lots(qty_shares) -> float:
    """Bot quantities are in shares (lots × lot size). Whole lots stay ints."""
    n = (qty_shares or 0) / NIFTY_LOT_SIZE
    return int(n) if float(n).is_integer() else round(n, 2)


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _blank_state(date: str) -> dict:
    return {"date": date, "seq": 0, "position": None,
            "day": {"realised": 0.0, "per_start_lot": 0.0, "trades_closed": 0}}


def _load_state() -> dict:
    today = _now().strftime("%Y-%m-%d")
    try:
        st = json.loads(STATE.read_text())
    except FileNotFoundError:
        return _blank_state(today)
    except Exception as e:
        logger.warning(f"signal_feed: state unreadable ({e}) — starting fresh")
        return _blank_state(today)
    if st.get("date") != today:                          # new day: keep seq and any open position, reset the day
        st["date"] = today
        st["day"] = _blank_state(today)["day"]
    return st


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def _append(st: dict, event: dict) -> dict:
    st["seq"] = int(st.get("seq", 0)) + 1
    now = _now()
    full = {"v": SCHEMA_VERSION, "id": uuid.uuid4().hex, "seq": st["seq"],
            "ts": now.isoformat(timespec="seconds"), "date": now.strftime("%Y-%m-%d"),
            "instrument": INSTRUMENT, "lot_size": NIFTY_LOT_SIZE, **event}
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX, "a") as f:
        f.write(json.dumps(full, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return full


def _guard(fn):
    """Run fn under the lock; never let an exception reach the bot."""
    def wrapped(*a, **k):
        if not ENABLED:
            return None
        try:
            with _lock:
                return fn(*a, **k)
        except Exception as e:                            # noqa: BLE001 — by design
            logger.warning(f"signal_feed.{fn.__name__} failed (trading unaffected): {e}")
            return None
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


# ── public API (called from main.py) ─────────────────────────────────────────

@_guard
def trade_entry(trade, spot: float, strangle: Optional[dict] = None, strength: Optional[str] = None) -> Optional[dict]:
    """A new position was opened. `trade` is the bot's TradeState. For a strangle pass
    strangle={ce_strike, ce_symbol, ce_price, pe_strike, pe_symbol, pe_price}. `strength` is the
    signal's grade (STRONG / MODERATE / STRANGLE): public posts size trades by it, not by the lots
    actually filled (which margin step-down and late-entry sizing can cut)."""
    st = _load_state()
    if st.get("position"):
        logger.warning(f"signal_feed: entry while position {st['position']['id']} still open — replacing it")
    lots = _lots(trade.lots)
    if trade.action == "STRANGLE" and strangle:
        legs = [
            {"leg": "CE", "strike": strangle["ce_strike"], "symbol": strangle["ce_symbol"],
             "side": "SELL", "price": _r(strangle["ce_price"]), "lots": lots},
            {"leg": "PE", "strike": strangle["pe_strike"], "symbol": strangle["pe_symbol"],
             "side": "SELL", "price": _r(strangle["pe_price"]), "lots": lots},
        ]
    else:
        leg = _LEGS_OF.get(trade.action, ("?",))[0]
        legs = [{"leg": leg, "strike": trade.strike, "symbol": trade.symbol,
                 "side": "SELL", "price": _r(trade.entry_premium), "lots": lots}]
    pos_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-{trade.action}"
    event = {
        "type": "ENTRY", "position_id": pos_id, "structure": trade.action, "legs": legs,
        "spot": _r(spot), "sl_spot": _r(trade.sl_spot_level), "lots_open": lots, "entry_lots": lots,
        "expiry": trade.expiry, "strength": strength,
    }
    if trade.action == "STRANGLE" and trade.sl_put:
        event["sl_spot_put"] = _r(trade.sl_put)
    if trade.hedge_symbol:
        event["hedge"] = {"strike": trade.hedge_strike, "symbol": trade.hedge_symbol,
                          "side": "BUY", "price": _r(trade.hedge_entry_premium), "lots": lots}
    if st.get("position"):
        event["replaced_position_id"] = st["position"]["id"]
    st["position"] = {"id": pos_id, "structure": trade.action, "entry_lots": lots,
                      "open": {l["leg"]: lots for l in legs}}
    out = _append(st, event)
    _save_state(st)
    return out


@_guard
def trade_add(trade, lots_added_shares: int, legs_added: list) -> Optional[dict]:
    """Pyramid add filled. legs_added = [{leg, strike, price}] for each leg that was added to."""
    st = _load_state()
    pos = st.get("position")
    added = _lots(lots_added_shares)
    if pos:
        for l in legs_added:
            pos["open"][l["leg"]] = pos["open"].get(l["leg"], 0) + added
    event = {
        "type": "ADD", "position_id": pos["id"] if pos else None, "structure": trade.action,
        "legs": [{**l, "side": "SELL", "price": _r(l.get("price")), "lots": added} for l in legs_added],
        "lots_added": added, "lots_open": _lots(trade.lots), "blended_entry": _r(trade.entry_premium),
    }
    out = _append(st, event)
    _save_state(st)
    return out


@_guard
def trade_exit(trade, exit_price, pnl: float, reason: str, spot: float) -> Optional[dict]:
    """Some or all of the position was closed. Called from main._record_trade_exit, which every
    exit path goes through. `trade.lots` is the slice closed (shares)."""
    st = _load_state()
    pos = st.get("position")
    legs = _LEGS_OF.get(trade.action, ())
    closed = _lots(trade.lots)
    qty = trade.lots or 0

    start_lots = None
    if pos:
        start_lots = pos.get("entry_lots") or None
        for leg in legs:
            if leg in pos["open"]:
                pos["open"][leg] = max(0, round(pos["open"][leg] - closed, 2))
        lots_open = max(pos["open"].values()) if pos["open"] else 0
        position_closed = lots_open == 0
    else:
        start_lots = _lots(trade.entry_lots) or None    # position unknown (feed was off, or state lost)
        lots_open = None
        position_closed = None

    # a strangle slice books both legs, so points are per share of ONE leg pair
    units = qty * (len(legs) if trade.action == "STRANGLE" else 1)
    pnl_per_start = (pnl / start_lots) if start_lots else None
    event = {
        "type": "EXIT", "position_id": pos["id"] if pos else None,
        "structure": pos["structure"] if pos else trade.action,
        "leg": "BOTH" if len(legs) > 1 else (legs[0] if legs else "?"),
        "strike": trade.strike, "exit_price": _r(exit_price), "spot": _r(spot),
        "lots_closed": closed, "lots_open": lots_open, "position_closed": position_closed,
        "reason": reason, "pnl": _r(pnl, 0),
        "points": _r(pnl / units) if units else None,
        "pnl_per_start_lot": _r(pnl_per_start, 0),
    }
    day = st["day"]
    day["realised"] += float(pnl)
    if pnl_per_start is not None:
        day["per_start_lot"] += pnl_per_start
    if position_closed:
        day["trades_closed"] += 1
        st["position"] = None
    out = _append(st, event)
    _save_state(st)
    return out


@_guard
def scan_result(final, spot: float) -> Optional[dict]:
    """An observe-only scan (before 10:00 or after the entry window). Only the bias is
    recorded — no strike or premium — so nothing here can be traded on before the bot trades."""
    st = _load_state()
    event = {
        "type": "SCAN", "action": final.action, "strength": final.strength, "score": final.score,
        "spot": _r(spot), "pcr": _r(getattr(final, "pcr", None)),
        "resistance": _r(getattr(final, "resistance_level", None), 0),
        "support": _r(getattr(final, "support_level", None), 0),
        "call_wall": getattr(final, "call_wall", None) or None,
        "put_wall": getattr(final, "put_wall", None) or None,
    }
    out = _append(st, event)
    _save_state(st)
    return out


def mark(unrealised: Optional[float], spot: Optional[float] = None) -> None:
    """Remember the latest unrealised P&L the bot computed. Memory only, cheap, called every scan."""
    global _mark
    if not ENABLED or unrealised is None:
        return
    _mark = {"unrealised": float(unrealised), "spot": spot, "at": _now()}


@_guard
def pnl_snapshot(day_realised: float, open_trade, label: str = "HOURLY") -> Optional[dict]:
    """Hourly P&L line. day_realised is the bot's own figure (main._day_realised_pnl)."""
    st = _load_state()
    pos = st.get("position")
    position_open = open_trade is not None
    unrealised = spot = age = None
    if position_open and _mark:
        age = (_now() - _mark["at"]).total_seconds()
        if age <= MARK_STALE_SEC:
            unrealised, spot = _mark["unrealised"], _mark["spot"]
    start_lots = (pos or {}).get("entry_lots") or (_lots(open_trade.entry_lots) if position_open else None)
    event = {
        "type": "PNL_SNAPSHOT", "label": label,
        "position_id": pos["id"] if pos else None,
        "structure": open_trade.action if position_open else None,
        "day_realised": _r(day_realised, 0),
        "unrealised": _r(unrealised, 0) if position_open else 0,
        "day_total": _r(day_realised + (unrealised or 0), 0) if (unrealised is not None or not position_open) else None,
        "mark_age_sec": int(age) if age is not None else None,
        "day_per_start_lot": _r(st["day"]["per_start_lot"]),          # paise kept: the poster multiplies
        "open_per_start_lot": _r(unrealised / start_lots) if (unrealised is not None and start_lots) else None,
        "trades_closed_today": st["day"]["trades_closed"],
        "position_open": position_open,
        "lots_open": _lots(open_trade.lots) if position_open else 0,
        "spot": _r(spot),
    }
    out = _append(st, event)
    _save_state(st)
    return out
