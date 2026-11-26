"""
Follower sizing — ONE calculation shared by X and Telegram, so both always show the same rupees,
and those rupees are what a follower really makes by following the posts in whole lots.

Size plans (lots by signal grade):
    STANDARD  STRONG 3 · MODERATE 2 · STRANGLE 2 per leg   (X, the Telegram channel, "Standard" DMs)
    SMALL     1 lot for every grade                         ("Small" DMs)

Whole lots: after every owner event (whatever lots the owner actually got filled), a follower's
open lots on each leg become
    target = round(size × owner_open / owner_start), an exact half rounding DOWN (= book the extra
             lot: profit taken sooner, less left at risk), and never below 1 while the owner still
             holds that leg (a 1-lot follower holds through partial bookings and exits with him).
The instruction is the difference between that target and what the follower already holds.

P&L is a ledger of what the follower actually closed: on every exit, the owner's real ₹ on that
slice (hedge included) per leg-lot he closed × the leg-lots the follower closed.
Pyramid adds use the owner's blended entry; if a follower's add had to be rounded, their figures
are marked "≈".
"""
from __future__ import annotations

import math
from typing import Optional

STANDARD = {"STRONG": 3, "MODERATE": 2, "STRANGLE": 2}
SMALL = {"STRONG": 1, "MODERATE": 1, "STRANGLE": 1}
PLANS = {"standard": STANDARD, "small": SMALL}
LOT_SIZE = 65


def lots_for(plan: dict, grade: Optional[str], structure: Optional[str]) -> int:
    if grade in plan:
        return plan[grade]
    if structure == "STRANGLE":
        return plan["STRANGLE"]
    return plan["MODERATE"]                     # unknown grade: treat as moderate (2 standard, 1 small)


def target(size: int, owner_open: float, owner_start: float) -> int:
    if owner_open <= 1e-9 or not owner_start:
        return 0
    return max(1, math.ceil(size * owner_open / owner_start - 0.5 - 1e-9))


def legs_of(ev: dict) -> list:
    """Which legs an EXIT/ADD event touches."""
    leg = ev.get("leg")
    if leg == "BOTH":
        return ["CE", "PE"]
    if leg in ("CE", "PE"):
        return [leg]
    return [l["leg"] for l in ev.get("legs", [])]


def new_position(ev: dict) -> dict:
    """Book-keeping for an ENTRY event, with every plan opened at its full size."""
    start = ev.get("entry_lots") or 0
    pos = {
        "id": ev.get("position_id"), "grade": ev.get("strength"), "structure": ev.get("structure"),
        "start": start, "owner_open": {l["leg"]: start for l in ev["legs"]},
        "legs": {l["leg"]: {"strike": l["strike"], "symbol": l.get("symbol"), "price": l.get("price")} for l in ev["legs"]},
        "hedge": ev.get("hedge"), "sl_spot": ev.get("sl_spot"), "sl_spot_put": ev.get("sl_spot_put"),
        "expiry": ev.get("expiry"), "opened": ev.get("ts"), "lot_size": ev.get("lot_size") or LOT_SIZE,
        "sizes": {},
    }
    for name in PLANS:
        size_state(pos, name)
    return pos


def size_state(pos: dict, plan_name: str) -> dict:
    if plan_name not in pos["sizes"]:
        n = lots_for(PLANS[plan_name], pos["grade"], pos["structure"])
        pos["sizes"][plan_name] = {"n": n, "open": {leg: n for leg in pos["owner_open"]}, "rs": 0.0, "exact": True}
    return pos["sizes"][plan_name]


def apply_owner(pos: dict, ev: dict) -> None:
    """Update the owner's open lots per leg from an ADD or EXIT event."""
    if ev["type"] == "ADD":
        for leg in legs_of(ev):
            pos["owner_open"][leg] = pos["owner_open"].get(leg, 0) + (ev.get("lots_added") or 0)
    elif ev["type"] == "EXIT":
        for leg in legs_of(ev):
            if leg in pos["owner_open"]:
                pos["owner_open"][leg] = max(0, round(pos["owner_open"][leg] - (ev.get("lots_closed") or 0), 4))
        if ev.get("position_closed"):
            for leg in pos["owner_open"]:
                pos["owner_open"][leg] = 0


def step(pos: dict, ev: dict) -> dict:
    """Apply one ADD/EXIT event to the owner and to every plan.
    Returns {plan: {"deltas": {leg: ±lots}, "slice_rs": ₹ realised on this event}}."""
    apply_owner(pos, ev)
    out = {}
    for name in PLANS:
        st = size_state(pos, name)
        deltas = {}
        for leg, owner_open in pos["owner_open"].items():
            want = target(st["n"], owner_open, pos["start"])
            have = st["open"].get(leg, 0)
            if want != have:
                deltas[leg] = want - have
                st["open"][leg] = want
            if ev["type"] == "ADD" and leg in legs_of(ev) and pos["start"]:
                if abs(want - st["n"] * owner_open / pos["start"]) > 1e-9:
                    st["exact"] = False                  # follower's add was rounded vs the owner's
        slice_rs = 0.0
        if ev["type"] == "EXIT":
            closed = sum(-d for d in deltas.values() if d < 0)
            owner_units = (ev.get("lots_closed") or 0) * len(legs_of(ev))     # owner leg-lots closed
            if ev.get("pnl") is not None and owner_units:
                slice_rs = ev["pnl"] * closed / owner_units                     # exact ₹ per leg-lot × follower's
            elif ev.get("points") is not None:
                slice_rs = ev["points"] * pos["lot_size"] * closed
            elif closed:
                st["exact"] = False
            st["rs"] += slice_rs
        out[name] = {"deltas": deltas, "slice_rs": slice_rs}
    return out


def realised(pos: dict, plan_name: str) -> float:
    return size_state(pos, plan_name)["rs"]


def open_value(pos: dict, plan_name: str, owner_unrealised: Optional[float]) -> Optional[float]:
    """A plan's unrealised ₹ from the owner's: per open leg-lot, times the plan's open leg-lots."""
    owner_total = sum(pos["owner_open"].values())
    if owner_unrealised is None or owner_total <= 0:
        return None
    return owner_unrealised / owner_total * sum(size_state(pos, plan_name)["open"].values())


def open_lots(pos: dict, plan_name: str) -> int:
    return max(size_state(pos, plan_name)["open"].values() or [0])
