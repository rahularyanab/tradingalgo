"""
The executor engine: turns API instructions into broker orders, and protects the account on its own.

Safety rules (each covered by tests/test_executor.py):
  1. Stale entries are skipped: an OPEN, or an add inside an ADJUST, older than stale_open_sec is not
     chased. Exits are never skipped.
  2. No new position outside entry_from–entry_until; everything is flattened at the instruction's
     flatten_by (15:25), retried every loop until flat.
  3. Margin-safe sequence: hedge BUY → main SELL when opening (a failed SELL unwinds the hedge it
     bought); main BUY-back → hedge SELL when closing.
  4. Never closes more than it holds; never lets a leg exceed max_lots_per_leg.
  5. A broker stop (BUY stop at entry × (1 + disaster_sl_pct)) protects each short while it is open,
     so a dead server or network can't leave it unprotected. It is cancelled BEFORE any planned
     buy-back (no double buy) and re-placed for whatever stays open.
  6. Local spot guard: if Nifty stays past the trade's stop for guard_grace_sec with no exit
     instruction, the executor closes the position itself.
  7. A close that didn't fully fill is retried every loop.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("executor")
IST = ZoneInfo("Asia/Kolkata")
LOT = 65


def now_ist() -> datetime:
    return datetime.now(IST)


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Engine:
    def __init__(self, settings, venue, api, state_path: Optional[Path] = None, clock: Callable[[], datetime] = now_ist):
        self.s, self.v, self.api, self.clock = settings, venue, api, clock
        self.state_path = Path(state_path or (settings.home / "executor_state.json"))
        self.st = self._load()
        self.events: list = []                               # human-readable log of what happened (for reports/tests)

    # ── state ────────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {"cursor": None, "positions": {}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        os.replace(tmp, self.state_path)

    def note(self, msg: str) -> None:
        logger.info(msg)
        self.events.append(msg)

    # ── the loop body ────────────────────────────────────────────────────────
    def start(self) -> dict:
        status = self.api.status()
        if self.st.get("cursor") is None:
            self.st["cursor"] = int(status.get("latest_seq") or 0)       # never replay old trades
            self.note(f"first start: following from instruction {self.st['cursor']}")
            self.save()
        return status

    def tick(self) -> None:
        now = self.clock()
        reply = self.api.instructions(self.st["cursor"])
        for ins in reply.get("instructions", []):
            try:
                self.process(ins, now)
            finally:
                self.st["cursor"] = max(self.st["cursor"], ins["seq"])
                self.save()
        self.st["cursor"] = max(self.st["cursor"], int(reply.get("next") or 0))
        self.guard(now)
        self.save()

    # ── instructions ─────────────────────────────────────────────────────────
    def process(self, ins: dict, now: datetime) -> None:
        pid = ins.get("position_id") or f"seq{ins['seq']}"
        age = (now - datetime.fromisoformat(ins["ts"]).astimezone(IST)).total_seconds()
        orders = ins.get("orders", [])
        opening = [o for o in orders if not o.get("closing")]
        closing = [o for o in orders if o.get("closing")]
        if ins.get("flatten_by"):
            self.st["flatten_by"] = ins["flatten_by"]

        if ins["kind"] == "OPEN":
            if pid in self.st["positions"]:
                return self.note(f"#{ins['seq']} OPEN {pid}: already open here — ignored")
            if age > self.s.stale_open_sec:
                return self.note(f"#{ins['seq']} OPEN skipped: {age:.0f}s old (limit {self.s.stale_open_sec}s) — never chase a stale entry")
            if not (_hm(self.s.entry_from) <= now.time() < _hm(self.s.entry_until)):
                return self.note(f"#{ins['seq']} OPEN skipped: outside {self.s.entry_from}–{self.s.entry_until}")
            pos = {"structure": ins.get("structure"), "opened": ins["ts"], "sl": ins.get("sl") or {},
                   "legs": {}, "hedges": {}, "stops": {}, "breach_since": None}
            self.st["positions"][pid] = pos
            self._open(ins, pos, opening)
            if not any(l["qty"] for l in pos["legs"].values()):
                self.st["positions"].pop(pid, None)
                self.note(f"#{ins['seq']} OPEN {pid}: nothing filled — no position")
            return

        pos = self.st["positions"].get(pid)
        if pos is None:
            if orders:
                self.note(f"#{ins['seq']} {ins['kind']} {pid}: not held here (entry was skipped) — nothing to do")
            return
        if opening:
            if age > self.s.stale_open_sec:
                self.note(f"#{ins['seq']} add skipped: {age:.0f}s old")
            else:
                self._open(ins, pos, opening)
        if closing:
            self._close(ins, pos, closing, why=ins.get("reason") or ins["kind"])
        if ins["kind"] == "CLOSE":
            pos["target"] = {leg: 0 for leg in pos["legs"]}
            self._close_all(pid, pos, "instruction says flat")
        self._drop_if_flat(pid)

    def _open(self, ins: dict, pos: dict, orders: list) -> None:
        hedge_got: dict = {}
        for o in orders:
            leg, qty = o["leg"], int(o["qty"])
            if o["role"] == "hedge":
                f = self.v.execute("BUY", o["symbol"], qty, o.get("ref_price"))
                hedge_got[leg] = hedge_got.get(leg, 0) + f.qty
                h = pos["hedges"].setdefault(leg, {"symbol": o["symbol"], "qty": 0})
                h["qty"] += f.qty
                self.note(f"#{ins['seq']} BUY hedge {o['symbol']} {f.qty}/{qty}" + (f" ({f.note})" if f.note else ""))
                continue
            held = pos["legs"].get(leg, {}).get("qty", 0)
            if held + qty > self.s.max_lots_per_leg * LOT:
                qty = max(0, self.s.max_lots_per_leg * LOT - held)
                self.note(f"#{ins['seq']} SELL {o['symbol']} cut to {qty} by the {self.s.max_lots_per_leg}-lot cap")
            if leg in hedge_got:                             # never more short than the hedge just bought
                qty = min(qty, hedge_got[leg])
            f = self.v.execute("SELL", o["symbol"], qty, o.get("ref_price"))
            self.note(f"#{ins['seq']} SELL {o['symbol']} {f.qty}/{qty}" + (f" ({f.note})" if f.note else ""))
            if f.qty:
                l = pos["legs"].setdefault(leg, {"symbol": o["symbol"], "qty": 0, "entry": None})
                prev = l["qty"]
                l["entry"] = ((l["entry"] or 0) * prev + (f.avg or o.get("ref_price") or 0) * f.qty) / (prev + f.qty)
                l["qty"] += f.qty
                self._protect(pos, leg)
            if leg in hedge_got and f.qty < hedge_got[leg]:  # SELL short of the hedge: unwind the extra hedge now
                extra = hedge_got[leg] - f.qty
                u = self.v.execute("SELL", pos["hedges"][leg]["symbol"], extra, None)
                pos["hedges"][leg]["qty"] -= u.qty
                self.note(f"#{ins['seq']} unwound {u.qty}/{extra} hedge {pos['hedges'][leg]['symbol']} (main SELL fell short)")

    def _close(self, ins_or_tag, pos: dict, orders: list, why: str) -> None:
        tag = f"#{ins_or_tag['seq']}" if isinstance(ins_or_tag, dict) else ins_or_tag
        for o in orders:
            leg = o["leg"]
            if o["role"] == "main":
                l = pos["legs"].get(leg)
                qty = min(int(o["qty"]), (l or {}).get("qty", 0))
                if not qty:
                    continue
                self.v.cancel_stop(pos["stops"].pop(leg, None))          # never let the stop double-buy
                want_left = l["qty"] - qty
                f = self.v.execute("BUY", l["symbol"], qty, o.get("ref_price"))
                l["qty"] -= f.qty
                if l["qty"] > want_left:                     # short fill: keep trying every loop (rule 7)
                    pos.setdefault("target", {})[leg] = want_left
                else:
                    pos.get("target", {}).pop(leg, None)
                self.note(f"{tag} BUY back {l['symbol']} {f.qty}/{qty} ({why})" + (f" — {f.note}" if f.note else ""))
                if l["qty"]:
                    self._protect(pos, leg)
            else:
                h = pos["hedges"].get(leg)
                main_left = pos["legs"].get(leg, {}).get("qty", 0)
                qty = min(int(o["qty"]), (h or {}).get("qty", 0), max(0, (h or {}).get("qty", 0) - main_left))
                if not qty:
                    continue                                 # keep the hedge while the short it covers is open
                f = self.v.execute("SELL", h["symbol"], qty, o.get("ref_price"))
                h["qty"] -= f.qty
                self.note(f"{tag} SELL hedge {h['symbol']} {f.qty}/{qty}")

    def _close_all(self, pid: str, pos: dict, why: str) -> None:
        orders = [{"leg": leg, "role": "main", "qty": l["qty"], "ref_price": None} for leg, l in pos["legs"].items() if l["qty"]]
        orders += [{"leg": leg, "role": "hedge", "qty": h["qty"], "ref_price": None} for leg, h in pos["hedges"].items() if h["qty"]]
        if orders:
            self._close(pid, pos, orders, why)

    def _protect(self, pos: dict, leg: str) -> None:
        l = pos["legs"][leg]
        self.v.cancel_stop(pos["stops"].pop(leg, None))
        if l["qty"] and l.get("entry"):
            oid = self.v.place_stop(l["symbol"], l["qty"], l["entry"] * (1 + self.s.disaster_sl_pct / 100))
            if oid:
                pos["stops"][leg] = oid

    def _drop_if_flat(self, pid: str) -> None:
        pos = self.st["positions"].get(pid)
        if pos and not any(l["qty"] for l in pos["legs"].values()) and not any(h["qty"] for h in pos["hedges"].values()):
            for oid in pos["stops"].values():
                self.v.cancel_stop(oid)
            self.st["positions"].pop(pid)
            self.note(f"{pid}: flat")

    # ── guards that run every loop ───────────────────────────────────────────
    def guard(self, now: datetime) -> None:
        if not self.st["positions"]:
            return
        flatten = _hm(self.st.get("flatten_by") or "15:25")
        if now.time() >= flatten:
            for pid, pos in list(self.st["positions"].items()):
                pos["target"] = {leg: 0 for leg in pos["legs"]}
                self._close_all(pid, pos, f"{flatten:%H:%M} flatten")
                self._drop_if_flat(pid)
            return
        for pid, pos in list(self.st["positions"].items()):        # rule 7: finish closes that fell short
            todo = [{"leg": leg, "role": "main", "qty": pos["legs"][leg]["qty"] - t, "ref_price": None}
                    for leg, t in (pos.get("target") or {}).items() if pos["legs"].get(leg, {}).get("qty", 0) > t]
            if todo:
                todo += [{"leg": o["leg"], "role": "hedge", "qty": pos["hedges"].get(o["leg"], {}).get("qty", 0), "ref_price": None}
                         for o in todo if pos["hedges"].get(o["leg"])]
                self._close(pid, pos, todo, "retrying an unfinished close")
                self._drop_if_flat(pid)
        spot = self.v.spot()
        for pid, pos in list(self.st["positions"].items()):
            sl = pos.get("sl") or {}
            breached = spot is not None and ((sl.get("spot_above") and spot > sl["spot_above"]) or
                                             (sl.get("spot_below") and spot < sl["spot_below"]))
            if not breached:
                pos["breach_since"] = None
                continue
            if not pos.get("breach_since"):
                pos["breach_since"] = now.isoformat(timespec="seconds")
                self.note(f"{pid}: Nifty {spot} is past the stop {sl} — waiting {self.s.guard_grace_sec}s for the exit instruction")
                continue
            if (now - datetime.fromisoformat(pos["breach_since"])).total_seconds() >= self.s.guard_grace_sec:
                pos["target"] = {leg: 0 for leg in pos["legs"]}
                self._close_all(pid, pos, f"local stop: Nifty {spot} past {sl}, no exit instruction")
                self._drop_if_flat(pid)
