"""
Offline tests for the public signal feed (publish/signal_feed.py) and its hooks in main.py.

Run from the repo root:   python3 tests/test_signal_feed.py
Every file is redirected to a temp dir; Telegram, the journal and the broker are faked — nothing is
posted and no order is possible from here.
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""     # never post to the live bot
import main
from publish import signal_feed as feed
from signals.trade_monitor import TradeState
from dataclasses import replace

TMP = Path(tempfile.mkdtemp())
main.JOURNAL_PATH = TMP / "journal.csv"
main._STATE_FILE = TMP / "state.json"
main.send_trade_journal_entry = lambda *a, **k: True
main._post = lambda text: True
main._register_exit_verify = lambda *a, **k: None
LOT = main.NIFTY_LOT_SIZE

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"   — {detail}" if detail and not cond else ""))


def reset():
    feed.OUTBOX = TMP / f"outbox_{len(PASS) + len(FAIL)}.jsonl"
    feed.STATE = TMP / f"state_{len(PASS) + len(FAIL)}.json"
    feed._mark = None
    main._day_realised_pnl = 0.0


def events():
    if not feed.OUTBOX.exists():
        return []
    return [json.loads(l) for l in feed.OUTBOX.read_text().splitlines()]


def directional(lots=6, action="CALL_SELL"):
    return TradeState(action=action, strike=24500, symbol="NIFTY24500CE", entry_time=datetime.now(),
                      entry_premium=100.0, entry_spot=24300, sl_spot_level=24450, expiry="2026-12-01",
                      lots=lots * LOT, hedge_symbol="NIFTY24800CE", hedge_strike=24800, hedge_entry_premium=10.0)


# ── 1. directional: entry → pyramid add → profit book → full exit ─────────────
print("directional lifecycle")
reset()
t = directional(6)
feed.trade_entry(t, 24300)
t.lots = 12 * LOT; t.entry_premium = 95.0
feed.trade_add(t, 6 * LOT, [{"leg": "CE", "strike": 24500, "price": 90.0}])
feed.trade_exit(replace(t, lots=2 * LOT), 60.0, 4500.0, "PROFIT_BOOK", 24250)
feed.trade_exit(replace(t, lots=10 * LOT), 70.0, 16250.0, "SIGNAL_EXIT", 24260)
ev = events()
check("four events written in order", [e["type"] for e in ev] == ["ENTRY", "ADD", "EXIT", "EXIT"], [e["type"] for e in ev])
check("seq is 1..4", [e["seq"] for e in ev] == [1, 2, 3, 4])
check("one position_id across the trade", len({e["position_id"] for e in ev}) == 1 and ev[0]["position_id"])
check("entry carries leg, price, lots, SL and hedge",
      ev[0]["legs"] == [{"leg": "CE", "strike": 24500, "symbol": "NIFTY24500CE", "side": "SELL", "price": 100.0, "lots": 6}]
      and ev[0]["sl_spot"] == 24450 and ev[0]["hedge"]["strike"] == 24800, ev[0])
check("add: +6 lots, 12 open, blended entry", ev[1]["lots_added"] == 6 and ev[1]["lots_open"] == 12 and ev[1]["blended_entry"] == 95.0)
check("profit book is partial: 10 lots still open", ev[2]["lots_open"] == 10 and ev[2]["position_closed"] is False)
check("points = pnl / shares closed", ev[2]["points"] == round(4500 / (2 * LOT), 2), ev[2]["points"])
check("per-start-lot P&L divides by the 6 starting lots", ev[2]["pnl_per_start_lot"] == 750, ev[2]["pnl_per_start_lot"])
check("final exit closes the position", ev[3]["lots_open"] == 0 and ev[3]["position_closed"] is True)
st = json.loads(feed.STATE.read_text())
check("state: flat, 1 trade closed, day tallies add up",
      st["position"] is None and st["day"]["trades_closed"] == 1
      and st["day"]["realised"] == 20750 and round(st["day"]["per_start_lot"]) == round(20750 / 6), st)

# ── 2. strangle: entry → CE leg SL → PE leg exit ──────────────────────────────
print("strangle lifecycle")
reset()
s = TradeState(action="STRANGLE", strike=24600, symbol="NIFTY24600CE + NIFTY24000PE", entry_time=datetime.now(),
               entry_premium=80.0, entry_spot=24300, sl_spot_level=24650, sl_put=23950, expiry="2026-12-01", lots=4 * LOT)
feed.trade_entry(s, 24300, strangle={"ce_strike": 24600, "ce_symbol": "NIFTY24600CE", "ce_price": 45.0,
                                     "pe_strike": 24000, "pe_symbol": "NIFTY24000PE", "pe_price": 35.0})
ce = replace(s, action="STRANGLE_CE", strike=24600)
pe = replace(s, action="STRANGLE_PE", strike=24000)
feed.trade_exit(ce, 70.0, -6500.0, "SL_HIT", 24660)
feed.trade_exit(pe, 5.0, 7800.0, "EOD", 24640)
ev = events()
check("strangle entry has both legs with their own prices",
      [(l["leg"], l["price"]) for l in ev[0]["legs"]] == [("CE", 45.0), ("PE", 35.0)] and ev[0]["sl_spot_put"] == 23950)
check("CE leg exit leaves the PE leg open", ev[1]["leg"] == "CE" and ev[1]["lots_open"] == 4 and ev[1]["position_closed"] is False, ev[1])
check("PE leg exit closes it", ev[2]["leg"] == "PE" and ev[2]["position_closed"] is True, ev[2])

print("strangle profit book (both legs at once)")
reset()
feed.trade_entry(s, 24300, strangle={"ce_strike": 24600, "ce_symbol": "C", "ce_price": 45.0,
                                     "pe_strike": 24000, "pe_symbol": "P", "pe_price": 35.0})
feed.trade_exit(replace(s, lots=2 * LOT), 30.0, 5200.0, "PROFIT_BOOK", 24300)
e = events()[-1]
check("books BOTH legs, 2 lots left on each", e["leg"] == "BOTH" and e["lots_open"] == 2 and e["position_closed"] is False, e)
check("points are per share of one leg", e["points"] == round(5200 / (2 * 2 * LOT), 2), e["points"])

# ── 3. hourly snapshots ───────────────────────────────────────────────────────
print("pnl snapshots")
reset()
t = directional(6)
feed.trade_entry(t, 24300)
feed.mark(-3900.0, 24410)
feed.pnl_snapshot(2000.0, t)
e = events()[-1]
check("open position: realised + fresh unrealised", e["type"] == "PNL_SNAPSHOT" and e["unrealised"] == -3900
      and e["day_total"] == -1900 and e["open_per_start_lot"] == -650 and e["lots_open"] == 6, e)
feed._mark["at"] = feed._now() - timedelta(minutes=45)
feed.pnl_snapshot(2000.0, t)
e = events()[-1]
check("stale mark is reported as unknown, never as a number", e["unrealised"] is None and e["day_total"] is None, e)
reset()
feed.pnl_snapshot(0.0, None)
e = events()[-1]
check("flat day: zero unrealised, position_open false", e["position_open"] is False and e["unrealised"] == 0
      and e["day_total"] == 0 and e["trades_closed_today"] == 0, e)

# ── 4. day rollover keeps seq, resets the day ─────────────────────────────────
print("day rollover")
reset()
feed.trade_entry(directional(6), 24300)
st = json.loads(feed.STATE.read_text())
st["date"] = "2000-01-01"; st["day"]["realised"] = 999
feed.STATE.write_text(json.dumps(st))
feed.pnl_snapshot(0.0, directional(6))
st = json.loads(feed.STATE.read_text())
check("new day resets tallies, keeps seq and open position",
      st["day"]["realised"] == 0 and st["seq"] == 2 and st["position"] is not None, st)

# ── 5. it can never hurt the bot ──────────────────────────────────────────────
print("failure safety")
reset()
feed.OUTBOX = TMP / "no_such_dir_file"
feed.OUTBOX.mkdir()                      # a directory where the file should be → every write fails
try:
    r1 = feed.trade_entry(directional(6), 24300)
    r2 = feed.trade_exit(directional(6), 50.0, 100.0, "SL_HIT", 24300)
    r3 = feed.pnl_snapshot(0.0, None)
    check("unwritable outbox: no exception, calls return None", (r1, r2, r3) == (None, None, None))
except Exception as ex:                  # noqa: BLE001
    check("unwritable outbox: no exception", False, repr(ex))
reset()
feed.trade_exit(directional(6), 50.0, 3250.0, "MANUAL_EXIT", 24300)
e = events()[-1]
check("exit with no known position still recorded, marked unknown",
      e["position_id"] is None and e["lots_open"] is None and e["position_closed"] is None and e["pnl_per_start_lot"] == 542, e)
reset()
feed.ENABLED = False
feed.trade_entry(directional(6), 24300)
check("SIGNAL_FEED=0 writes nothing", events() == [])
feed.ENABLED = True

# ── 6. wired into main.py: every exit funnels through _record_trade_exit ──────
print("main.py hooks")
reset()
t = directional(6)
feed.trade_entry(t, 24300)
main._record_trade_exit(t, 40.0, 5.0, 23400.0, -1950.0, 21450.0, 24200, "TARGET")
e = events()[-1]
check("_record_trade_exit emits EXIT with net P&L", e["type"] == "EXIT" and e["pnl"] == 21450 and e["reason"] == "TARGET"
      and e["position_closed"] is True, e)
main.monitor.trade = None
main.feed_pnl_snapshot()
e = events()[-1]
weekday = datetime.now().weekday() < 5
check("feed_pnl_snapshot uses the bot's day realised (weekdays)",
      (e["type"] == "PNL_SNAPSHOT" and e["day_realised"] == 21450) if weekday else e["type"] == "EXIT", e)
import inspect
src = inspect.getsource(main)
check("entries hooked (strangle + 2 directional)", src.count("_feed.trade_entry(") == 3)
check("pyramid adds hooked (strangle + directional)", src.count("_feed.trade_add(") == 2)
check("marks hooked (scan + guard)", src.count("_feed.mark(") == 2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
