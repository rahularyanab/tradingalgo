"""
Paper Trading Engine
====================
Tracks virtual positions using live option LTPs from Zerodha.
No real orders are placed. P&L is computed from live market prices.

Each 15-min scan:
  - Fetches live LTP for every open position leg
  - Computes unrealised P&L
  - Applies all real SL / target / management rules
  - Logs everything to CSV journal
  - Sends Telegram P&L update
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import NIFTY_LOT_SIZE, SL_BUFFER_POINTS, STRANGLE_SL_BUFFER, TARGET_DECAY_PCT

logger = logging.getLogger(__name__)

JOURNAL_DIR = Path(__file__).parent.parent / "logs"
JOURNAL_DIR.mkdir(exist_ok=True)


@dataclass
class PaperLeg:
    option_type:     str      # "CE" | "PE"
    strike:          int
    symbol:          str      # NFO symbol e.g. NIFTY2662624500CE
    lots:            int
    entry_premium:   float
    entry_time:      datetime
    active:          bool     = True
    exit_premium:    Optional[float]   = None
    exit_time:       Optional[datetime]= None
    exit_reason:     Optional[str]     = None

    @property
    def realised_pnl(self) -> Optional[float]:
        if self.exit_premium is None:
            return None
        return round((self.entry_premium - self.exit_premium) * self.lots * NIFTY_LOT_SIZE, 2)


@dataclass
class PaperPosition:
    position_id:  int
    action:       str        # "CALL_SELL" | "PUT_SELL" | "STRANGLE"
    entry_time:   datetime
    entry_spot:   float
    expiry:       str
    sl_call:      Optional[float]    # call SL spot level
    sl_put:       Optional[float]    # put SL spot level
    legs:         list = field(default_factory=list)   # list[PaperLeg]
    is_closed:    bool = False

    @property
    def active_legs(self) -> list:
        return [l for l in self.legs if l.active]

    @property
    def total_entry_premium(self) -> float:
        return sum(l.entry_premium for l in self.legs)

    @property
    def total_realised_pnl(self) -> float:
        return sum(l.realised_pnl or 0 for l in self.legs if not l.active)


class PaperTrader:
    def __init__(self, kite):
        self.kite       = kite
        self.positions: list[PaperPosition] = []
        self._id_counter = 0
        self._init_journal()

    def _journal_file(self) -> Path:
        """Always uses today's date — survives midnight / multi-day service runs."""
        return JOURNAL_DIR / f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv"

    # ── Journal ───────────────────────────────────────────────────

    def _init_journal(self):
        jf = self._journal_file()
        if not jf.exists():
            with open(jf, "w", newline="") as f:
                csv.writer(f).writerow([
                    "id", "action", "leg_type", "strike", "symbol", "lots",
                    "entry_time", "entry_premium", "entry_spot",
                    "exit_time", "exit_premium", "exit_reason", "pnl",
                ])

    def _log_leg(self, pos: PaperPosition, leg: PaperLeg):
        jf = self._journal_file()
        if not jf.exists():
            self._init_journal()   # new day — create header
        with open(jf, "a", newline="") as f:
            csv.writer(f).writerow([
                pos.position_id, pos.action, leg.option_type, leg.strike,
                leg.symbol, leg.lots,
                leg.entry_time.strftime("%Y-%m-%d %H:%M"),
                leg.entry_premium, pos.entry_spot,
                leg.exit_time.strftime("%Y-%m-%d %H:%M") if leg.exit_time else "",
                leg.exit_premium or "",
                leg.exit_reason or "",
                leg.realised_pnl or "",
            ])

    # ── Live LTP ──────────────────────────────────────────────────

    def get_ltp(self, symbol: str) -> Optional[float]:
        try:
            data = self.kite.ltp(f"NFO:{symbol}")
            return data[f"NFO:{symbol}"]["last_price"]
        except Exception as e:
            logger.warning(f"LTP fetch failed for {symbol}: {e}")
            return None

    def get_unrealised_pnl(self, leg: PaperLeg) -> tuple[float, Optional[float]]:
        """Returns (unrealised_pnl, current_premium)."""
        current = self.get_ltp(leg.symbol)
        if current is None:
            return 0.0, None
        pnl = round((leg.entry_premium - current) * leg.lots * NIFTY_LOT_SIZE, 2)
        return pnl, current

    # ── Position management ───────────────────────────────────────

    @property
    def open_position(self) -> Optional[PaperPosition]:
        for p in self.positions:
            if not p.is_closed and p.active_legs:
                return p
        return None

    def enter_directional(self, action: str, strike: int, symbol: str,
                          premium: float, lots: int, spot: float,
                          sl_spot: float, expiry: str) -> PaperPosition:
        self._id_counter += 1
        leg = PaperLeg(
            option_type="CE" if action == "CALL_SELL" else "PE",
            strike=strike, symbol=symbol, lots=lots,
            entry_premium=premium, entry_time=datetime.now(),
        )
        pos = PaperPosition(
            position_id=self._id_counter,
            action=action, entry_time=datetime.now(),
            entry_spot=spot, expiry=expiry,
            sl_call=sl_spot if action == "CALL_SELL" else None,
            sl_put =sl_spot if action == "PUT_SELL"  else None,
            legs=[leg],
        )
        self.positions.append(pos)
        self._log_leg(pos, leg)
        logger.info(f"[PAPER] ENTER {action} {strike} @ ₹{premium}  lots={lots}  sl={sl_spot}")
        return pos

    def enter_strangle(self, ce_strike: int, ce_symbol: str, ce_premium: float,
                       pe_strike: int, pe_symbol: str, pe_premium: float,
                       lots: int, spot: float,
                       sl_call: float, sl_put: float, expiry: str) -> PaperPosition:
        self._id_counter += 1
        ce_leg = PaperLeg("CE", ce_strike, ce_symbol, lots, ce_premium, datetime.now())
        pe_leg = PaperLeg("PE", pe_strike, pe_symbol, lots, pe_premium, datetime.now())
        pos = PaperPosition(
            position_id=self._id_counter,
            action="STRANGLE", entry_time=datetime.now(),
            entry_spot=spot, expiry=expiry,
            sl_call=sl_call, sl_put=sl_put,
            legs=[ce_leg, pe_leg],
        )
        self.positions.append(pos)
        self._log_leg(pos, ce_leg)
        self._log_leg(pos, pe_leg)
        logger.info(
            f"[PAPER] ENTER STRANGLE CE={ce_strike}@₹{ce_premium} "
            f"PE={pe_strike}@₹{pe_premium}  lots={lots}"
        )
        return pos

    def exit_leg(self, pos: PaperPosition, leg: PaperLeg, reason: str,
                 ltp_override: Optional[float] = None) -> float:
        """Exit one leg. Returns realised P&L for that leg."""
        current = ltp_override if ltp_override is not None else self.get_ltp(leg.symbol)
        if current is None:
            current = leg.entry_premium   # fallback: flat P&L
        leg.exit_premium = current
        leg.exit_time    = datetime.now()
        leg.exit_reason  = reason
        leg.active       = False
        pnl = leg.realised_pnl or 0
        self._log_leg(pos, leg)
        logger.info(
            f"[PAPER] EXIT {leg.option_type} {leg.strike} @ ₹{current}  "
            f"entry=₹{leg.entry_premium}  pnl=₹{pnl}  reason={reason}"
        )
        if not pos.active_legs:
            pos.is_closed = True
        return pnl

    def exit_all_legs(self, pos: PaperPosition, reason: str,
                      ltp_map: Optional[dict] = None) -> float:
        """Exit all remaining legs. Returns total realised P&L."""
        total = 0.0
        for leg in pos.active_legs:
            override = ltp_map.get(leg.symbol) if ltp_map else None
            total += self.exit_leg(pos, leg, reason, ltp_override=override)
        pos.is_closed = True
        return total

    def check_sl_and_target(self, pos: PaperPosition, spot: float, oc=None) -> Optional[str]:
        """
        Check if SL or target hit for current position.
        Returns exit reason string if should exit, else None.
        Pass oc (OptionChainData) to use already-fetched LTPs for the target check
        instead of making separate kite.ltp() calls that can fail on symbol format changes.
        """
        if pos.is_closed or not pos.active_legs:
            return None

        # SL check uses spot price only — no LTP needed
        for leg in pos.active_legs:
            if leg.option_type == "CE" and pos.sl_call and spot >= pos.sl_call:
                return "SL_CALL"
            if leg.option_type == "PE" and pos.sl_put and spot <= pos.sl_put:
                return "SL_PUT"

        # Target check: prefer option chain LTP, fall back to kite.ltp()
        total_entry   = sum(l.entry_premium for l in pos.legs)
        target_prem   = total_entry * (1 - TARGET_DECAY_PCT)
        current_prems = []
        for leg in pos.active_legs:
            cur = None
            if oc is not None:
                chain = oc.call_data if leg.option_type == "CE" else oc.put_data
                sd = chain.get(leg.strike)
                if sd and sd.ltp > 0:
                    cur = sd.ltp
            if cur is None:
                cur = self.get_ltp(leg.symbol)
            if cur is not None:
                current_prems.append(cur)

        if current_prems and sum(current_prems) <= target_prem:
            return "TARGET"

        return None

    # ── Summary ───────────────────────────────────────────────────

    def get_position_summary(self, pos: PaperPosition, spot: float, oc=None) -> dict:
        """
        Current state of the position for Telegram reporting.
        Pass oc (OptionChainData) to read LTPs from the already-fetched option chain
        instead of making separate kite.ltp() calls (avoids symbol format issues).
        """
        legs_info = []
        total_unrealised = 0.0

        for leg in pos.active_legs:
            # Prefer option chain LTP — avoids symbol format mismatches with kite.ltp()
            cur_prem = None
            if oc is not None:
                chain = oc.call_data if leg.option_type == "CE" else oc.put_data
                sd = chain.get(leg.strike)
                if sd and sd.ltp > 0:
                    cur_prem = sd.ltp

            if cur_prem is None:
                _, cur_prem = self.get_unrealised_pnl(leg)   # fallback to kite.ltp()

            pnl = round((leg.entry_premium - cur_prem) * leg.lots * NIFTY_LOT_SIZE, 2) if cur_prem else 0.0
            total_unrealised += pnl
            legs_info.append({
                "type":    leg.option_type,
                "strike":  leg.strike,
                "entry":   leg.entry_premium,
                "current": cur_prem,
                "pnl":     pnl,
                "lots":    leg.lots,
            })

        total_realised = pos.total_realised_pnl

        closed_legs_info = [
            {
                "type":   leg.option_type,
                "strike": leg.strike,
                "entry":  leg.entry_premium,
                "exit":   leg.exit_premium,
                "pnl":    leg.realised_pnl or 0,
                "lots":   leg.lots,
            }
            for leg in pos.legs
            if not leg.active and leg.exit_premium is not None
        ]

        return {
            "position_id":       pos.position_id,
            "action":            pos.action,
            "entry_spot":        pos.entry_spot,
            "current_spot":      spot,
            "expiry":            pos.expiry,
            "legs":              legs_info,
            "closed_legs":       closed_legs_info,
            "unrealised_pnl":    total_unrealised,
            "realised_pnl":      total_realised,
            "total_pnl":         total_unrealised + total_realised,
            "sl_call":           pos.sl_call,
            "sl_put":            pos.sl_put,
        }

    def session_summary(self) -> dict:
        """End-of-day summary of all paper trades."""
        all_pnl = sum(
            sum(l.realised_pnl or 0 for l in p.legs)
            for p in self.positions
        )
        def pos_pnl(p):
            return sum(l.realised_pnl or 0 for l in p.legs)

        wins   = [p for p in self.positions if pos_pnl(p) > 0]
        losses = [p for p in self.positions if pos_pnl(p) < 0]
        flat   = [p for p in self.positions if pos_pnl(p) == 0]
        return {
            "total_trades":  len(self.positions),
            "winners":       len(wins),
            "losers":        len(losses),
            "flat":          len(flat),
            "total_pnl":     all_pnl,
            "journal_file":  str(self._journal_file()),
        }
