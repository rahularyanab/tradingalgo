"""
Executor instructions: the owner's trades turned into exact broker orders for each size plan,
for subscribers' executors (their own server, their own broker login) to place.

Built from the same signal outbox and the same whole-lot ledger (publish/follow.py) as X and the
Telegram channel, so an executor trades exactly what the posts say.

One instruction per owner event that changes positions:
    {"seq", "src_seq", "ts", "kind": OPEN | ADJUST | CLOSE, "position_id", "structure", "grade",
     "expiry", "reason", "sl": {"spot_above", "spot_below"}, "flatten_by": "15:25",
     "plans": {plan: {"orders": [order...], "open_after": {leg: lots}}}}
    order = {"exchange": "NFO", "symbol", "side": BUY | SELL, "lots", "qty", "role": main | hedge,
             "leg", "strike", "ref_price", "closing": bool}
Order sequence is margin-safe: when opening, the hedge is BOUGHT before the main leg is SOLD;
when closing, the main leg is BOUGHT BACK before the hedge is SOLD.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import paths as _paths
from publish import follow as F

INSTRUCTIONS: Path = _paths.LOGS_DIR / "exec_instructions.jsonl"
STATE: Path = _paths.LOGS_DIR / "exec_state.json"
FLATTEN_BY = "15:25"


def _orders(pos: dict, deltas: dict, prices: dict) -> list:
    lot = pos.get("lot_size") or F.LOT_SIZE
    hedge = pos.get("hedge") if pos.get("structure") != "STRANGLE" else None
    opening, closing = [], []
    for leg, d in deltas.items():
        main = {"exchange": "NFO", "symbol": pos["legs"][leg]["symbol"], "role": "main", "leg": leg,
                "strike": pos["legs"][leg]["strike"], "lots": abs(d), "qty": abs(d) * lot,
                "ref_price": prices.get(leg)}
        if d > 0:                                            # more short: hedge first, then sell
            if hedge:
                opening.append({"exchange": "NFO", "symbol": hedge["symbol"], "role": "hedge", "leg": leg,
                                "strike": hedge["strike"], "lots": d, "qty": d * lot, "ref_price": hedge.get("price"),
                                "side": "BUY", "closing": False})
            opening.append({**main, "side": "SELL", "closing": False})
        else:                                                # less short: buy back, then release the hedge
            closing.append({**main, "side": "BUY", "closing": True})
            if hedge:
                closing.append({"exchange": "NFO", "symbol": hedge["symbol"], "role": "hedge", "leg": leg,
                                "strike": hedge["strike"], "lots": -d, "qty": -d * lot, "ref_price": None,
                                "side": "SELL", "closing": True})
    return opening + closing


def _sl(pos: dict) -> dict:
    if pos.get("structure") == "STRANGLE":
        return {"spot_above": pos.get("sl_spot"), "spot_below": pos.get("sl_spot_put")}
    leg = next(iter(pos["legs"]))
    return {"spot_above": pos.get("sl_spot")} if leg == "CE" else {"spot_below": pos.get("sl_spot")}


def build(ev: dict, st: dict) -> Optional[dict]:
    """The instruction for one outbox event (None if it changes no positions)."""
    positions = st.setdefault("positions", {})
    t = ev["type"]
    pid = ev.get("position_id")
    if t == "ENTRY":
        pos = F.new_position(ev)
        positions[pid or "?"] = pos
        plans = {}
        for name in F.PLANS:
            n = F.size_state(pos, name)["n"]
            deltas = {leg: n for leg in pos["legs"]}
            plans[name] = {"orders": _orders(pos, deltas, {l["leg"]: l.get("price") for l in ev["legs"]}),
                           "open_after": dict(F.size_state(pos, name)["open"])}
        kind, reason = "OPEN", "ENTRY"
    elif t in ("ADD", "EXIT"):
        pos = positions.get(pid or "")
        if pos is None:
            return None                                      # never saw it open: nothing safe to instruct
        res = F.step(pos, ev)
        legs = F.legs_of(ev)
        if t == "ADD":
            prices = {l["leg"]: l.get("price") for l in ev.get("legs", [])}
        else:
            prices = {leg: (ev.get("exit_price") if len(legs) == 1 else None) for leg in legs}
        plans = {name: {"orders": _orders(pos, r["deltas"], prices),
                        "open_after": dict(F.size_state(pos, name)["open"])} for name, r in res.items()}
        closed = t == "EXIT" and bool(ev.get("position_closed"))
        kind, reason = ("CLOSE" if closed else "ADJUST"), (ev.get("reason") or t)
        if closed:
            positions.pop(pid, None)
    else:
        return None
    st["iseq"] = int(st.get("iseq", 0)) + 1
    return {"seq": st["iseq"], "src_seq": ev["seq"], "ts": ev["ts"], "kind": kind, "position_id": pid,
            "opened": pos.get("opened"), "structure": pos.get("structure"), "grade": pos.get("grade"),
            "expiry": pos.get("expiry"), "reason": reason, "sl": _sl(pos), "flatten_by": FLATTEN_BY,
            "plans": plans}


def _load_state() -> Optional[dict]:
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return None


def _save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def update(outbox: Optional[Path] = None) -> int:
    """Append instructions for new outbox events. First run starts at the end (no replay).
    Returns how many instructions were added."""
    outbox = outbox or _paths.SIGNAL_OUTBOX
    events = []
    if outbox.exists():
        for line in outbox.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    st = _load_state()
    if st is None:
        st = {"seq": max((e.get("seq", 0) for e in events), default=0), "iseq": 0, "positions": {}}
        STATE.parent.mkdir(parents=True, exist_ok=True)
        _save_state(st)
        return 0
    added = 0
    for ev in sorted(events, key=lambda e: e.get("seq", 0)):
        if ev.get("seq", 0) <= st["seq"]:
            continue
        ins = build(ev, st)
        if ins:
            with open(INSTRUCTIONS, "a") as f:
                f.write(json.dumps(ins) + "\n")
                f.flush()
                os.fsync(f.fileno())
            added += 1
        st["seq"] = ev["seq"]
        _save_state(st)
    return added


def read_since(since: int, limit: int = 200) -> list:
    if not INSTRUCTIONS.exists():
        return []
    out = []
    for line in INSTRUCTIONS.read_text().splitlines():
        try:
            ins = json.loads(line)
        except ValueError:
            continue
        if ins["seq"] > since:
            out.append(ins)
            if len(out) >= limit:
                break
    return out
